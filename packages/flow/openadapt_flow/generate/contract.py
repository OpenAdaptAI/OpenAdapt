"""Generate a record contract from what varied demonstrations did to the store.

Input: two or three demonstrations of the same save, each with the values a
person typed and the system of record read before and after it, through one
read channel. Output: a ``verify.yaml`` (``openadapt.verify/v1``) contract:

- **the delta**: the record each demonstration added (or changed), with every
  field that equals a typed value in every demonstration bound to that
  parameter, and every field that held one constant value in every
  demonstration pinned as a literal;
- **the frame**: the whole read set, compared by the field that identifies a
  record; fields that changed on records the save never touched, in every
  demonstration alike, are ignored as volatile (never one a parameter is
  written to);
- **side records**: records the app adds next to the target on every save,
  declared by what they share across demonstrations.

The contract is then refined by execution, not by reading it again: the fault
gate (``openadapt_flow/fault_gate.py``) writes bad saves to a test copy, and
each surviving fault class adds the check that kills it, until the suite is
clean or nothing more can be added (the survivors are reported). This is the
single-pass-versus-grounded lesson of SpecMind: a contract that merely looks
right is not admitted until it has been seen to fail.

Nothing here weakens a check. :func:`openadapt_flow.generate.baseline`
refuses any result below the floor, and field values are data: a value that
reads like an instruction is just a value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

#: Fields commonly assigned by the store; tried first as record identity.
_IDENTITY_CANDIDATES = ("id", "uuid", "pid", "_id", "record_id", "resource_id")


class ContractError(ValueError):
    """The demonstrations don't determine a contract. Nothing is proposed."""


@dataclass
class Demonstration:
    """One save as the read channel saw it."""

    params: dict[str, str]
    before: list[dict[str, Any]]
    after: list[dict[str, Any]]


@dataclass
class FieldRole:
    field: str
    #: ``param`` (bound to a typed value), ``literal`` (constant), ``derived``
    #: (varies, matches nothing typed: an id, a timestamp), ``ambiguous``.
    role: str
    param: Optional[str] = None
    literal: Optional[str] = None
    note: str = ""


@dataclass
class GeneratedContract:
    """A proposed contract and the evidence behind each part of it."""

    verify: dict[str, Any]
    identity_field: str
    roles: list[FieldRole]
    match: list[str]
    volatile: list[str] = field(default_factory=list)
    side_records: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refinements: list[str] = field(default_factory=list)

    def bound(self) -> dict[str, str]:
        return {r.field: r.param for r in self.roles if r.role == "param" and r.param}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm(value: Any) -> str:
    return " ".join(_text(value).split()).casefold()


def identity_field_of(demos: Sequence[Demonstration]) -> str:
    """The field that identifies a record in every snapshot.

    It must be on every record, unique within each snapshot, and unchanged
    on every record present both before and after a demonstration.
    """

    def candidates() -> list[str]:
        common: Optional[set[str]] = None
        for demo in demos:
            for record in demo.before + demo.after:
                keys = set(record)
                common = keys if common is None else common & keys
        names = sorted(common or set())
        preferred = [n for n in _IDENTITY_CANDIDATES if n in names]
        return preferred + [n for n in names if n not in preferred]

    for name in candidates():
        ok = True
        for demo in demos:
            for snapshot in (demo.before, demo.after):
                values = [_text(r.get(name)) for r in snapshot]
                if "" in values or len(set(values)) != len(values):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            return name
    raise ContractError(
        "no field identifies a record in every snapshot (present, unique, stable)"
    )


def _index(records: Sequence[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {_text(r.get(key)): r for r in records}


@dataclass
class _Delta:
    added: list[dict[str, Any]]
    changed: list[tuple[dict[str, Any], dict[str, Any]]]
    removed: list[dict[str, Any]]


def _delta(demo: Demonstration, key: str) -> _Delta:
    before = _index(demo.before, key)
    after = _index(demo.after, key)
    added = [after[k] for k in after if k not in before]
    removed = [before[k] for k in before if k not in after]
    changed = [
        (before[k], after[k]) for k in after if k in before and before[k] != after[k]
    ]
    return _Delta(added, changed, removed)


def _holds_params(record: Mapping[str, Any], params: Mapping[str, str]) -> int:
    values = {_norm(v) for v in params.values() if _norm(v)}
    return sum(1 for v in record.values() if _norm(v) in values)


def _target(demo: Demonstration, delta: _Delta, key: str) -> tuple[dict[str, Any], str]:
    """The record the save wrote: the added (or changed) one holding the most
    typed values."""

    pool = [(r, "added") for r in delta.added] + [
        (after, "changed") for _b, after in delta.changed
    ]
    scored = sorted(
        ((r, how, _holds_params(r, demo.params)) for r, how in pool),
        key=lambda t: -t[2],
    )
    if not scored or scored[0][2] == 0:
        raise ContractError(
            "a demonstration added or changed no record holding any typed value"
        )
    if len(scored) > 1 and scored[1][2] == scored[0][2]:
        raise ContractError(
            "two records hold the typed values equally; the save is ambiguous"
        )
    return scored[0][0], scored[0][1]


def field_roles(
    targets: Sequence[dict[str, Any]],
    demos: Sequence[Demonstration],
    key: str,
) -> list[FieldRole]:
    """What each field of the saved record is, across the demonstrations."""

    roles: list[FieldRole] = []
    names = sorted(set().union(*(set(t) for t in targets)))
    for name in names:
        if name == key:
            roles.append(FieldRole(name, "derived", note="record identity"))
            continue
        values = [_norm(t.get(name)) for t in targets]
        bound = [
            p
            for p in demos[0].params
            if all(_norm(d.params.get(p)) == v and v for d, v in zip(demos, values))
        ]
        if len(bound) == 1:
            roles.append(FieldRole(name, "param", param=bound[0]))
        elif len(bound) > 1:
            roles.append(
                FieldRole(
                    name,
                    "ambiguous",
                    note="equals more than one typed value: "
                    + ", ".join(sorted(bound))
                    + " (vary them in the demonstrations)",
                )
            )
        elif len(set(values)) == 1:
            if len(demos) < 2:
                roles.append(
                    FieldRole(
                        name, "derived", note="one demonstration: constant unknown"
                    )
                )
            else:
                roles.append(
                    FieldRole(name, "literal", literal=_text(targets[0].get(name)))
                )
        else:
            roles.append(
                FieldRole(name, "derived", note="varies and matches nothing typed")
            )
    return roles


def _volatile(
    demos: Sequence[Demonstration], key: str, targets: Sequence[str]
) -> list[str]:
    """Fields that changed on untouched records in every demonstration."""

    per_demo: list[set[str]] = []
    for demo, target in zip(demos, targets):
        moved: set[str] = set()
        for before, after in _delta(demo, key).changed:
            if _text(after.get(key)) == target:
                continue
            moved |= {
                f for f in set(before) | set(after) if before.get(f) != after.get(f)
            }
        per_demo.append(moved)
    if not per_demo:
        return []
    common = set.intersection(*per_demo)
    return sorted(common)


def _match_fields(
    roles: Sequence[FieldRole], demos: Sequence[Demonstration], key: str
) -> list[str]:
    """The fewest bound fields that pick the target out of every snapshot."""

    param_of = {r.field: r.param for r in roles if r.role == "param" and r.param}
    bound = list(param_of)
    if not bound:
        raise ContractError("no field of the saved record equals a typed value")

    def unique(fields: Sequence[str]) -> bool:
        for demo in demos:
            want = {f: _norm(demo.params[param_of[f]]) for f in fields}
            hits = [
                r for r in demo.after if all(_norm(r.get(f)) == want[f] for f in fields)
            ]
            if len(hits) != 1:
                return False
        return True

    # Prefer fields whose values are most distinct across the read set, then
    # the longer ones (more information per value: an email over a date).
    def distinct(field_name: str) -> int:
        return len({_norm(r.get(field_name)) for d in demos for r in d.after})

    def length(field_name: str) -> float:
        values = [_norm(r.get(field_name)) for d in demos for r in d.after]
        return sum(map(len, values)) / max(1, len(values))

    ordered = sorted(bound, key=lambda f: (-distinct(f), -length(f), f))
    for f in ordered:
        if unique([f]):
            return [f]
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if unique([a, b]):
                return sorted([a, b])
    return sorted(ordered)


def generate_contract(
    demos: Sequence[Demonstration],
    channel: Mapping[str, Any],
    *,
    name: str = "record",
    timeout_s: float = 5.0,
) -> GeneratedContract:
    """Propose a contract from ``demos`` read through ``channel``.

    ``channel`` is the verifier section the read channel generator admitted.
    """

    if len(demos) < 2:
        raise ContractError(
            "at least two demonstrations with different values are needed: one "
            "can't tell a typed value from a constant"
        )
    names = [set(d.params) for d in demos]
    if any(n != names[0] for n in names):
        raise ContractError("the demonstrations typed different parameters")
    unvaried = sorted(
        p for p in names[0] if len({_norm(d.params[p]) for d in demos}) == 1
    )
    if len(unvaried) == len(names[0]):
        raise ContractError(
            "every demonstration typed the same values; vary them, or a typed "
            "value can't be told from a constant"
        )
    key = identity_field_of(demos)
    targets: list[dict[str, Any]] = []
    notes: list[str] = []
    side_counts: list[int] = []
    for demo in demos:
        delta = _delta(demo, key)
        target, _how = _target(demo, delta, key)
        targets.append(target)
        side_counts.append(len(delta.added) - (1 if target in delta.added else 0))
        if delta.removed:
            raise ContractError(
                "a demonstration removed records; a save that deletes is not "
                "generated (a person writes that contract)"
            )
    roles = field_roles(targets, demos, key)
    for p in unvaried:
        notes.append(
            f"every demonstration typed the same {p!r}; a field holding it is "
            "checked against the typed value, but whether it is a constant can't "
            "be told (vary it to find out)"
        )
    for role in roles:
        if role.role == "ambiguous":
            notes.append(f"{role.field}: {role.note}")
    unbound = [
        p for p in sorted(names[0]) if p not in {r.param for r in roles if r.param}
    ]
    for p in unbound:
        notes.append(
            f"typed value {p!r} is not stored as typed in the saved record; its "
            "conversion is generated with the input types, or a person maps it"
        )
    match = _match_fields(roles, demos, key)
    volatile = [
        f
        for f in _volatile(demos, key, [_text(t.get(key)) for t in targets])
        if f not in {r.field for r in roles if r.role in ("param", "literal")}
    ]

    def expr(role: FieldRole) -> Any:
        return (
            {"param": role.param} if role.role == "param" else {"literal": role.literal}
        )

    by_field = {r.field: r for r in roles}
    selector = {f: expr(by_field[f]) for f in match}
    effects: list[dict[str, Any]] = [
        {
            "kind": "record_written",
            "match": selector,
            "expected_count": 1,
            "count_new_only": True,
            "probe": f"exactly one new {name} record with the task's values",
            "timeout_s": timeout_s,
        }
    ]
    for role in roles:
        if role.field in match or role.role not in ("param", "literal"):
            continue
        effects.append(
            {
                "kind": "field_equals",
                "match": selector,
                "field": role.field,
                "value": expr(role),
                "probe": f"{name} field {role.field!r} read back",
                "timeout_s": timeout_s,
            }
        )
    if len(set(side_counts)) != 1:
        raise ContractError(
            "the demonstrations added different numbers of records; the save's "
            "side records can't be declared"
        )
    if side_counts[0]:
        notes.append(
            f"each save also added {side_counts[0]} other record(s); they are "
            "not declared, so the frame refutes them until a person declares them"
        )
    verify: dict[str, Any] = {
        "schema_version": "openadapt.verify/v1",
        "verifier": dict(channel),
        "effects": effects,
        "frame": {"identity_field": key, "ignore_fields": volatile},
    }
    return GeneratedContract(
        verify=verify,
        identity_field=key,
        roles=roles,
        match=match,
        volatile=volatile,
        notes=notes,
    )


# -- refinement by execution ------------------------------------------------------


def refine(
    contract: GeneratedContract,
    survivors: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Add the check each surviving fault calls for. Returns what was added.

    Only additions: a survivor never removes or loosens a check.
    """

    added: list[str] = []
    effects = contract.verify["effects"]
    fields_checked = {
        e.get("field") for e in effects if e.get("kind") == "field_equals"
    } | set(contract.match)
    roles = {r.field: r for r in contract.roles}
    for case in survivors:
        fault = case.get("fault")
        detail = str(case.get("detail", ""))
        if fault in ("wrong_value", "partial"):
            for name, role in roles.items():
                if (
                    name in detail
                    and name not in fields_checked
                    and role.role
                    in (
                        "param",
                        "literal",
                    )
                ):
                    value = (
                        {"param": role.param}
                        if role.role == "param"
                        else {"literal": role.literal}
                    )
                    effects.append(
                        {
                            "kind": "field_equals",
                            "match": dict(effects[0]["match"]),
                            "field": name,
                            "value": value,
                            "probe": f"field {name!r} read back (added: a wrong "
                            "value survived)",
                            "timeout_s": effects[0].get("timeout_s", 5.0),
                        }
                    )
                    fields_checked.add(name)
                    added.append(f"field_equals {name}")
        elif fault == "duplicate" and not effects[0].get("count_new_only"):
            effects[0]["count_new_only"] = True
            added.append("count only new records")
        elif fault == "delayed":
            for e in effects:
                e["timeout_s"] = max(float(e.get("timeout_s", 5.0)), 10.0)
            added.append("wait longer for a late write")
    contract.refinements.extend(added)
    return added


def frame_scope_problem(survivors: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """An unrelated edit that survives means the read can't see the slice."""

    if any(c.get("fault") == "unrelated_edit" for c in survivors):
        return (
            "an edit to another record survived: the read channel does not "
            "return the whole slice; generate a wider read"
        )
    return None


@dataclass
class GatedContract:
    contract: GeneratedContract
    coverage: Any  # openadapt_flow.fault_gate.CoverageRecord
    rounds: int
    baseline_problems: list[str]
    unresolved: list[str]

    @property
    def admitted(self) -> bool:
        return (
            not self.baseline_problems
            and not self.unresolved
            and self.coverage is not None
            and self.coverage.complete
        )


def generate_and_gate(
    demos: Sequence[Demonstration],
    channel: Mapping[str, Any],
    copy_: Any,
    params: Mapping[str, str],
    *,
    max_rounds: int = 3,
    name: str = "record",
    timeout_s: float = 5.0,
    workdir: Any = None,
) -> GatedContract:
    """Generate, check the floor, run the fault gate, refine; repeat.

    ``copy_`` is a :class:`openadapt_flow.fault_gate.TestCopy` whose clean
    save writes what a demonstration wrote, with ``params``.
    """

    from openadapt_flow.fault_gate import run_gate
    from openadapt_flow.generate.baseline import baseline_problems
    from openadapt_flow.verify_run import parse_verify_config

    contract = generate_contract(demos, channel, name=name, timeout_s=timeout_s)
    coverage = None
    rounds = 0
    unresolved: list[str] = []
    problems: list[str] = []
    for rounds in range(1, max_rounds + 1):
        cfg = parse_verify_config(contract.verify, source="generated contract")
        problems = baseline_problems(cfg, demos[0].params)
        if problems:
            break
        coverage = run_gate(cfg, params, copy_, workdir=workdir)
        survivors = [c for c in coverage.cases if c["result"] == "SURVIVED"]
        if not survivors and coverage.clean_pass:
            unresolved = []
            break
        unresolved = []
        if not coverage.clean_pass:
            unresolved.append("the clean save does not verify on the test copy")
        scope = frame_scope_problem(survivors)
        if scope:
            unresolved.append(scope)
        if not refine(contract, survivors):
            unresolved += [
                f"{c['fault']}: {c['detail']}"
                for c in survivors
                if c["fault"] != "unrelated_edit"
            ]
            break
    return GatedContract(contract, coverage, rounds, problems, unresolved)
