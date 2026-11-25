"""Formal UI State Machine (PortalFSM) compiler and pre-execution safety model checker.

Compiles passive shadow traces into a typed finite-state machine over portal states
and wire-verified transitions, then statically verifies three safety properties
before any click is executed:
  1. No Double-Commit: no reachable path may traverse a mutating commit edge twice
     for the same obligation without an explicit idempotency key.
  2. Consent Dominance: every path reaching a consent-gated commit edge must carry
     a verified capability token for that scope.
  3. Subject Isolation: every opaque literal bound on a commit edge must originate
     from the active episode's subject_key (blocking cross-patient/tenant leaks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from openadapt_kernel.copilot import ShadowTrace
from openadapt_kernel.ledger import ObligationLedger


@dataclass(frozen=True)
class FSMState:
    state_id: str
    origin: str
    url_pattern: str
    dom_invariants: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FSMTransition:
    transition_id: str
    from_state: str
    to_state: str
    action_label: str
    http_method: str = "GET"
    is_commit: bool = False
    idempotency_key: Optional[str] = None
    required_consent_scope: Optional[str] = None
    bound_fields: Tuple[str, ...] = ()


@dataclass
class SafetyViolation:
    property_name: str
    detail: str
    path: List[str] = field(default_factory=list)


@dataclass
class SafetyProof:
    safe: bool
    checked_properties: List[str]
    violations: List[SafetyViolation] = field(default_factory=list)


@dataclass
class PortalFSM:
    fsm_id: str
    initial_state: str
    states: Dict[str, FSMState] = field(default_factory=dict)
    transitions: List[FSMTransition] = field(default_factory=list)

    def add_state(self, state: FSMState) -> None:
        self.states[state.state_id] = state

    def add_transition(self, tr: FSMTransition) -> None:
        self.transitions.append(tr)

    @classmethod
    def compile_from_traces(
        cls,
        fsm_id: str,
        traces: Sequence[ShadowTrace],
        *,
        required_consent_scope: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> "PortalFSM":
        """Induce a typed 3-state (ENTRY -> STAGED -> VERIFIED_COMMIT) FSM from shadow traces."""
        if not traces:
            raise ValueError("compile_from_traces requires at least one ShadowTrace")
        origin = traces[0].portal_origin
        readback = traces[0].readback_url
        fields = tuple(sorted({k for t in traces for k in t.submitted_fields.keys()}))

        fsm = cls(fsm_id=fsm_id, initial_state="ENTRY")
        fsm.add_state(FSMState("ENTRY", origin, origin, ("form_mounted",)))
        fsm.add_state(FSMState("STAGED", origin, origin, ("fields_staged", "zero_dirty_mismatch")))
        fsm.add_state(FSMState("VERIFIED_COMMIT", origin, readback, ("independent_readback_verified",)))
        fsm.add_transition(
            FSMTransition(
                transition_id="stage_form",
                from_state="ENTRY",
                to_state="STAGED",
                action_label="stage_form",
                http_method="NONE",
                is_commit=False,
                bound_fields=fields,
            )
        )
        fsm.add_transition(
            FSMTransition(
                transition_id="commit_and_readback",
                from_state="STAGED",
                to_state="VERIFIED_COMMIT",
                action_label="submit",
                http_method="POST",
                is_commit=True,
                idempotency_key=idempotency_key,
                required_consent_scope=required_consent_scope,
                bound_fields=fields,
            )
        )
        return fsm


def verify_fsm_safety(
    fsm: PortalFSM,
    *,
    ledger: Optional[ObligationLedger] = None,
    planned_bindings: Optional[Dict[str, str]] = None,
    opaque_fields: Optional[Sequence[str]] = None,
    active_subject_key: Optional[str] = None,
    literal_subjects: Optional[Dict[str, str]] = None,
    max_depth: int = 12,
) -> SafetyProof:
    """Statically verify No Double-Commit, Consent Dominance, and Subject Isolation."""
    violations: List[SafetyViolation] = []
    checked = ["no_double_commit", "consent_dominance", "subject_isolation"]

    adj: Dict[str, List[FSMTransition]] = {}
    for tr in fsm.transitions:
        adj.setdefault(tr.from_state, []).append(tr)

    # Bounded DFS over all reachable paths up to max_depth to detect double-commit cycles
    stack: List[Tuple[str, List[str], Set[str]]] = [(fsm.initial_state, [], set())]
    while stack:
        state_id, path, committed_without_key = stack.pop()
        if len(path) >= max_depth:
            continue
        for tr in adj.get(state_id, []):
            next_path = [*path, tr.transition_id]
            next_committed = set(committed_without_key)
            if tr.is_commit:
                if tr.transition_id in committed_without_key and not tr.idempotency_key:
                    violations.append(
                        SafetyViolation(
                            property_name="no_double_commit",
                            detail=f"Commit edge {tr.transition_id!r} reachable twice without idempotency_key",
                            path=next_path,
                        )
                    )
                    continue
                if not tr.idempotency_key:
                    next_committed.add(tr.transition_id)
                if tr.required_consent_scope:
                    if ledger is None or not ledger.has_consent(tr.required_consent_scope):
                        violations.append(
                            SafetyViolation(
                                property_name="consent_dominance",
                                detail=(
                                    f"Commit edge {tr.transition_id!r} requires consent scope "
                                    f"{tr.required_consent_scope!r}"
                                ),
                                path=next_path,
                            )
                        )
            # Avoid infinite non-commit self-loops while still exploring commit re-entries
            if next_path.count(tr.transition_id) <= 2:
                stack.append((tr.to_state, next_path, next_committed))

    # Property 3: Subject isolation + SSA provenance check on planned commit bindings
    if planned_bindings and opaque_fields:
        if ledger is not None:
            for pv in ledger.verify_ssa_provenance(planned_bindings, opaque_fields):
                violations.append(
                    SafetyViolation(
                        property_name="subject_isolation",
                        detail=f"Field {pv.field_name!r} failed SSA provenance: {pv.reason}",
                    )
                )
        if active_subject_key and literal_subjects:
            for fname in opaque_fields:
                if fname in planned_bindings:
                    owner = literal_subjects.get(fname)
                    if owner is not None and owner != active_subject_key:
                        violations.append(
                            SafetyViolation(
                                property_name="subject_isolation",
                                detail=(
                                    f"Cross-subject taint on {fname!r}: belongs to {owner!r}, "
                                    f"active subject is {active_subject_key!r}"
                                ),
                            )
                        )

    return SafetyProof(safe=not violations, checked_properties=checked, violations=violations)
