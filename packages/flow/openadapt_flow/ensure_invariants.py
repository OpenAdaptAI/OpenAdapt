"""Optional global predicates and the production targeted admission boundary.

The independent global callable does not grant admission. The production
boundary requires the shared targeted reader's authenticated observations;
per-field comparisons remain separate and unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from openadapt_flow.hard_invariants import (
    HardInvariantVerdict,
    InvariantResult,
    judge_hard_invariants,
)

if TYPE_CHECKING:
    from openadapt_flow.ensure_record import (
        Baseline,
        Change,
        EnsureContract,
        EnsureResult,
    )
    from openadapt_flow.runtime.effects.targeted import TargetedObservation


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
        transient_ok,
    )

    rtype = contract.resource_type
    selves = {_ref(a) for a in contract.self_actors}
    mine = {
        r: c
        for r, c in changes.items()
        if not c.attributed or not c.authors or c.authors & selves
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
        scope_complete=audit_complete,
        deletes_complete=audit_complete,
        history_complete=key_history_complete,
        search_complete=search_complete,
    )


def enforce_ensure_invariants(
    result: EnsureResult,
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    changes: Mapping[str, Change],
    after_matches: Mapping[str, Mapping[str, Any]],
    *,
    observation: TargetedObservation | None = None,
) -> EnsureResult:
    """Gate production admission without changing the independent field judge.

    The shared targeted producer must bind receipt targets and the declared
    searches before this gate can hold. The optional global historical judge
    remains independent; unobserved outside-ID history is not a mandatory gate.
    """
    from dataclasses import replace

    from openadapt_flow.ensure_record import EnsureResult, _ref
    from openadapt_flow.frame_invariants import judge_frame_invariants
    from openadapt_flow.runtime.effects.frame import FrameVerdict

    observed = judge_ensure_invariants(
        contract, params, baseline, changes, after_matches
    )
    verdict = judge_frame_invariants(
        FrameVerdict(
            "held"
            if result.outcome in {"created_verified", "found_existing"}
            else "not_checked",
            "independent ensure field/operation judgment",
        ),
        observation,
    )
    # Preserve existing adjudicated scope/delete failures. A duplicate only
    # seen in wider history remains part of the optional global predicate.
    known_failures: dict[str, InvariantResult] = {
        check.name: check
        for check in observed.checks
        if check.status == "refuted" and check.name != "at_most_one"
    }
    verdict = replace(
        verdict,
        checks=tuple(known_failures.get(check.name, check) for check in verdict.checks),
    )
    selves = {_ref(actor) for actor in contract.self_actors}
    transient = sum(
        ref.startswith(contract.resource_type + "/")
        and {"C", "D"} <= change.actions
        and (
            not change.attributed or not change.authors or bool(change.authors & selves)
        )
        for ref, change in changes.items()
    )
    if transient and contract.operations.cardinality.own_transient:
        # Actor equality and search absence do not bind a resource to this
        # run's write receipts. No such authenticated input exists here.
        verdict = replace(
            verdict,
            checks=tuple(
                replace(
                    check,
                    status="refuted",
                    reason="own_transient_receipt_binding_missing",
                    observed_count=transient,
                )
                if check.name in {"declared_scope", "no_undeclared_deletes"}
                else check
                for check in verdict.checks
            ),
        )
    evidence = {**result.evidence, "hard_invariants": verdict.evidence()}
    reasons = list(result.reasons)
    outcome, identity = result.outcome, result.record_id
    if verdict.status == "refuted":
        evidence["invariants_refuted"] = True
        reasons.extend(
            f"hard_invariant:{check.name}:{check.reason}"
            for check in verdict.checks
            if check.status == "refuted"
        )
        outcome, identity = "needs_review", None
    elif verdict.status != "held" and outcome in {"created_verified", "found_existing"}:
        evidence["invariants_unknown"] = True
        reasons.extend(
            f"hard_invariant:{check.name}:{check.reason}"
            for check in verdict.checks
            if check.status == "not_checked"
        )
        outcome, identity = "needs_review", None
    return EnsureResult(outcome, identity, reasons, evidence)
