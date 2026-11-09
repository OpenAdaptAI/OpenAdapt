"""Adapt ensure-record's authoritative read observations to hard invariants.

This callable does not change legacy admission or per-field comparisons.
History/search completeness must be asserted by the reader after its complete
reads. An audit-free sweep cannot prove that no record was deleted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from openadapt_flow.hard_invariants import HardInvariantVerdict, judge_hard_invariants

if TYPE_CHECKING:
    from openadapt_flow.ensure_record import Baseline, Change, EnsureContract


def judge_ensure_invariants(
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    changes: Mapping[str, Change],
    after_matches: Mapping[str, Mapping[str, Any]],
    *,
    history_complete: bool = False,
    search_complete: bool = False,
) -> HardInvariantVerdict:
    """Three named predicates, with no I/O, model calls or semantic judgment.

    Declared bookkeeping, attribution and transient cleanup use their existing
    checks. Declared cleanup is excluded from scope/delete violations only
    after its historical versions, actors and reads have been proved safe.
    The history predicate still counts every observed business-key record,
    including a deleted duplicate. This callable does not admit a run.
    """

    from openadapt_flow.ensure_record import (
        Unreadable,
        _ref,
        bookkeeping_only,
        correction_refused,
        is_person,
        read_back_unbound,
        transient_ok,
        window_versions_unbound,
    )

    rtype = contract.resource_type
    selves = {_ref(a) for a in contract.self_actors}
    # Unknown attribution blocks a proof; it does not prove an own violation.
    unresolved = {r: c for r, c in changes.items() if not c.attributed or not c.authors}
    mine = {
        r: c for r, c in changes.items() if r not in unresolved and c.authors & selves
    }
    protected = {f"{rtype}/{rid}" for rid in (*baseline.matches, *after_matches)}
    protected |= {r for r, c in mine.items() if "C" in c.actions}
    if contract.operations.bookkeeping:
        mine = {
            r: c
            for r, c in mine.items()
            if not bookkeeping_only(contract, c, protected, selves)
        }
    creates = {
        r for r, c in mine.items() if r.startswith(rtype + "/") and "C" in c.actions
    }
    cleanup = set()
    if contract.operations.cardinality.own_transient:
        cleanup = {
            r
            for r in creates
            if r.split("/", 1)[1] not in after_matches
            and transient_ok(contract, mine[r], selves, params, changes)
        }
    remaining = creates - cleanup
    target = next(iter(remaining)) if len(remaining) == 1 else None
    if baseline.matches:
        # An ensure-record task that found its entity may write nothing.
        outside = len(mine)
    else:
        outside = sum(r != target and r not in cleanup for r in mine)
        if target is not None and "U" in mine[target].actions:
            if correction_refused(contract, mine[target], selves, params) is not None:
                outside += 1
    undeclared_deletes = sum(
        "D" in c.actions and r not in cleanup for r, c in mine.items()
    )

    audit_complete = (
        history_complete is True
        and contract.change_feed.audit is not None
        and not any(c.unlogged for c in changes.values())
    )
    keys = {f"{rtype}/{rid}" for rid in (*baseline.matches, *after_matches)}
    key_history_complete = (
        audit_complete
        and contract.change_feed.history
        and any(
            all(params.get(p) not in (None, "") for p in rule)
            for rule in contract.identity
        )
    )
    for ref, change in changes.items():
        if not ref.startswith(rtype + "/"):
            continue
        if (
            change.versions is None
            or isinstance(change.versions, bool)
            or change.versions != len(change.bodies)
            or bool(change.actions & {"C", "U"})
            and not change.bodies
        ):
            key_history_complete = False
        if "D" in change.actions and not change.bodies and change.prior is None:
            key_history_complete = False
        # A canonical metadata check cannot turn absent/partial history into
        # proof: read_back_unbound deliberately skips versions=None for legacy
        # callers. Validate history independently before using either helper.
        valid_window = (
            type(change.versions) is int
            and change.versions == len(change.bodies)
            and bool(change.bodies)
            and all(
                isinstance(body, Mapping)
                and body.get("resourceType") == rtype
                and body.get("id") == ref.split("/", 1)[1]
                for body in change.bodies
            )
        )
        if valid_window:
            try:
                unbound = window_versions_unbound(change)
                rid = ref.split("/", 1)[1]
                if unbound is None and rid in after_matches:
                    unbound = read_back_unbound(change, after_matches[rid])
                if unbound is not None:
                    key_history_complete = False
            except (Unreadable, TypeError, ValueError):
                key_history_complete = False
        else:
            key_history_complete = False
        bodies = list(change.bodies)
        if change.prior is not None:
            bodies.append(change.prior)
        for body in bodies:
            if (
                not isinstance(body, Mapping)
                or body.get("resourceType") != rtype
                or body.get("id") != ref.split("/", 1)[1]
            ):
                key_history_complete = False
                continue
            try:
                if is_person(contract, body, params):
                    keys.add(ref)
            except (Unreadable, TypeError, ValueError):
                key_history_complete = False
    return judge_hard_invariants(
        outside_scope_changes=outside,
        undeclared_deletes=undeclared_deletes,
        before_matches=len(baseline.matches),
        after_matches=len(after_matches),
        history_matches=len(keys),
        scope_complete=audit_complete and not unresolved,
        deletes_complete=audit_complete
        and not any("D" in change.actions for change in unresolved.values()),
        history_complete=key_history_complete,
        search_complete=search_complete,
    )
