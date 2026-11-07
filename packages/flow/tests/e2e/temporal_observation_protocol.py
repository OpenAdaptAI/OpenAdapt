"""Neutral inputs and actual-observation facts for temporal verifier adapters.

This module defines a small test protocol only. An adapter must execute the
named events against its real entrypoint and report facts it observed; it must
not fill in outcomes from the scenario's expected events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Sequence, cast

Phase = Literal[
    "before_begin",
    "baseline_read",
    "after_begin",
    "pre_dispatch",
    "dispatch",
    "post_dispatch",
    "before_end",
    "check_read",
    "settle",
    "after_end",
    "recheck",
    "final_gap",
]
Actor = Literal["authorized", "foreign"]
Effect = Literal[
    "noop",
    "set_target",
    "remove_target",
    "set_collateral",
    "revert_target",
]
Outcome = Literal["confirmed", "refuted", "uncertain"]
FrameVerdict = Literal["held", "refuted", "not_checked"]
TargetState = Literal["absent", "present", "unknown"]
ClaimOperation = Literal["new_write", "ensure_state"]
TemporalRequirement = Literal["historical_commit", "current_state", "through_barrier"]
EvidenceChannel = Literal[
    "direct_response", "native_operation", "trusted_log", "quiescent_diff"
]
EffectDisposition = Literal[
    "new_commit", "already_satisfied", "accepted_pending", "rejected", "unknown"
]
ReadFreshness = Literal["authoritative", "stale", "unknown"]
ReadbackSemantics = Literal["authoritative_current", "immutable_version"]
TemporalRelation = Literal[
    "unrelated_record_added", "evidence_removed", "same_record_conflict"
]
TemporalFault = Literal[
    "fail_note_once_after_recheck",
    "drop_submit_result",
    "restart_before_recheck",
]
ScopeVariant = Literal[
    "matching",
    "narrower_exemptions",
    "broader_exemptions",
    "incomparable",
]


@dataclass(frozen=True)
class TemporalEvent:
    """One explicit synthetic action at a named point in the real lifecycle."""

    stage: Phase
    actor: Actor
    effect: Effect


@dataclass(frozen=True)
class TemporalScenario:
    """Expected task and ordered interventions; intent never creates a writer."""

    initial_target: Literal["absent", "present"]
    intent: Literal["create", "update"]
    events: tuple[TemporalEvent, ...] = ()
    faults: tuple[TemporalFault, ...] = ()
    participating_scope_variants: tuple[ScopeVariant, ...] = ()
    initial_table_rows: int | None = None
    row_cap: int | None = None


@dataclass(frozen=True)
class VerdictSnapshot:
    """Separate transport reply, canonical verifier, frame, and durable ledger."""

    phase: Phase
    transport_status: int | None
    reply_state: str | None
    reply_outcome: Outcome | None
    reply_verified: bool | None
    canonical_outcome: Outcome | None
    frame_verdict: FrameVerdict | None
    durable_outcome: Outcome | None
    durable_state: str | None
    claim_ref_id: str | None = None
    manual_review_required: bool | None = None


@dataclass(frozen=True)
class ClaimRef:
    """Immutable claim facts obtained from the actual admitted write contract.

    Adapters must not construct these values from TemporalScenario intent or
    actor labels. References are opaque identifiers or digests, never raw
    account names, record contents, or response bodies.
    """

    claim_id: str
    attempt_ref: str
    session_ref: str
    principal_ref: str | None
    profile_ref: str
    source_tree_sha256: str
    operation: ClaimOperation
    temporal_requirement: TemporalRequirement
    target_ref: str
    dependency_scope: tuple[str, ...] | None
    expected_effect_sha256: str
    window_start_ref: str
    window_end_ref: str | None = None
    required_version_ref: str | None = None
    barrier_ref: str | None = None


@dataclass(frozen=True)
class ClaimEvidence:
    """Actual evidence facts from the owned actuator and independent reader.

    ``profile_admitted`` is the observed result of source-bound qualification,
    not an adapter's interpretation of the profile or a scenario expectation.
    A direct response may bind through the captured owned exchange; a separate
    native stream needs a joined native operation reference. Effect fields are
    normalized from the actual channel, not inferred from the scenario.
    """

    phase: Phase
    claim_ref_id: str
    attempt_ref: str
    session_ref: str
    principal_ref: str | None
    source_tree_sha256: str
    channel: EvidenceChannel | None
    profile_ref: str | None
    profile_admitted: bool | None
    semantics_sha256: str | None
    owned_exchange_ref: str | None
    request_ref: str | None
    response_ref: str | None
    effect_disposition: EffectDisposition | None
    effect_target_ref: str | None
    effect_version_ref: str | None
    native_operation_ref: str | None
    readback_semantics: ReadbackSemantics | None
    readback_target_ref: str | None
    readback_version_ref: str | None
    readback_effect_sha256: str | None
    readback_freshness: ReadFreshness
    readback_complete: bool | None
    window_complete: bool | None


@dataclass(frozen=True)
class ScopeWitness:
    """Actual snapshot scope and table representation at one read boundary.

    ``read_scope_sha256`` and its participant counterparts hash the complete
    non-exemption read scope. The full config hashes and finite exemption sets
    remain separately visible so a narrower allowlist can be distinguished
    from a different database/table/read scope.
    """

    phase: Phase
    read_index: int | None
    read_performed: bool
    read_config_sha256: str | None
    participating_config_sha256: tuple[str, ...]
    read_scope_sha256: str | None
    participating_scope_sha256: tuple[str, ...]
    read_exemptions: tuple[str, ...] | None
    participating_exemptions: tuple[tuple[str, ...], ...]
    tables: tuple["TableReadWitness", ...]


@dataclass(frozen=True)
class TableReadWitness:
    """Observed representation and row-cap coverage for one real table read."""

    table_id: str
    mode: str
    columns: tuple[str, ...]
    cell_columns: tuple[str, ...]
    row_count: int
    row_cap: int | None
    coverage: Literal["complete", "reference_columns", "append_only", "unknown"]
    row_hash_format: str


@dataclass(frozen=True)
class GapObservation:
    """Actual decision comparing two indexed snapshot reads."""

    before_read_index: int | None
    after_read_index: int
    verdict: Outcome | None
    clean: bool | None
    unread: bool | None


@dataclass(frozen=True)
class ObservedMutation:
    """A committed mutation seen by the adapter, not inferred from the input."""

    stage: Phase
    actor: Actor
    effect: Effect
    committed: bool
    target_ref: str | None = None


@dataclass(frozen=True)
class TemporalObservation:
    """Normalized facts returned by an adapter after driving its real API."""

    phase_trace: tuple[Phase, ...]
    begin_reached: bool
    mutations: tuple[ObservedMutation, ...]
    write_count: int
    verdicts: tuple[VerdictSnapshot, ...]
    scope_witnesses: tuple[ScopeWitness, ...]
    gaps: tuple[GapObservation, ...]
    observed_dispatch_effect: bool | None
    verifier_attributed_effect: bool | None
    ledger_present: bool | None
    observed_target_state: TargetState | None
    claim_ref: ClaimRef | None = None
    claim_evidence: tuple[ClaimEvidence, ...] = ()


class TemporalInvariantError(AssertionError):
    """An actual adapter observation violated the shared temporal contract."""


class TemporalAdapterUnavailable(RuntimeError):
    """The adapter cannot execute a requested scenario without faking support."""


def compatible_history_outcome(
    claim_holds: Sequence[bool],
) -> Outcome:
    """Test-oracle rule over explicit compatible histories, never adapter input.

    An empty candidate set is inconsistent evidence, not a vacuously true
    universal claim. Mixed histories are unresolved. Production adapters must
    report actual channel facts and must not supply this oracle result.
    """

    if not claim_holds:
        return "uncertain"
    if all(claim_holds):
        return "confirmed"
    if not any(claim_holds):
        return "refuted"
    return "uncertain"


def _latest_canonical_outcome(
    observation: TemporalObservation,
) -> Outcome | None:
    return next(
        (
            snapshot.canonical_outcome
            for snapshot in reversed(observation.verdicts)
            if snapshot.canonical_outcome is not None
        ),
        None,
    )


def temporal_relation_violations(
    before: TemporalObservation,
    after: TemporalObservation,
    relation: TemporalRelation,
) -> tuple[str, ...]:
    """Check a paired, actually observed perturbation of one immutable claim.

    The adapters supply both observations from real entrypoints. ``relation``
    identifies the property under test; it never supplies evidence to either
    observation.
    """

    before_claim = before.claim_ref
    after_claim = after.claim_ref
    if (
        before_claim is None
        or after_claim is None
        or before_claim.claim_id != after_claim.claim_id
        or before_claim != after_claim
    ):
        return ("paired_claim_unavailable_or_changed",)
    before_outcome = _latest_canonical_outcome(before)
    after_outcome = _latest_canonical_outcome(after)
    if before_outcome is None or after_outcome is None:
        return ("paired_canonical_outcome_unavailable",)

    if relation == "evidence_removed":
        before_facts = set(before.claim_evidence)
        after_facts = set(after.claim_evidence)
        if not after_facts.issubset(before_facts):
            return ("evidence_removal_pair_invalid",)
        if before_outcome == "uncertain" and after_outcome != "uncertain":
            return ("less_evidence_strengthened_verdict",)
        if before_outcome in ("confirmed", "refuted") and after_outcome not in (
            before_outcome,
            "uncertain",
        ):
            return ("less_evidence_flipped_verdict",)
        return ()

    if relation == "unrelated_record_added":
        if not before_claim.target_ref or before_claim.dependency_scope is None:
            return ("unrelated_record_scope_unavailable",)
        before_mutations = set(before.mutations)
        added = tuple(item for item in after.mutations if item not in before_mutations)
        if not added or any(item.target_ref is None for item in added):
            return ("unrelated_record_effect_unavailable",)
        relevant_refs = {before_claim.target_ref, *before_claim.dependency_scope}
        if any(item.target_ref in relevant_refs for item in added):
            return ("unrelated_record_pair_has_relevant_write",)
        if after_outcome != before_outcome:
            return ("unrelated_record_changed_verdict",)
        return ()

    if relation == "same_record_conflict":
        if before_claim.temporal_requirement == "historical_commit":
            return ()
        prior_mutations = set(before.mutations)
        conflicts = tuple(
            item
            for item in after.mutations
            if item not in prior_mutations
            and item.committed
            and (item.actor == "foreign" or item.stage != "dispatch")
            and item.target_ref == before_claim.target_ref
        )
        if not conflicts:
            return ("same_record_conflict_unobserved",)
        latest = next(
            (
                snapshot
                for snapshot in reversed(after.verdicts)
                if snapshot.canonical_outcome is not None
            ),
            None,
        )
        if after_outcome != "uncertain":
            return ("same_record_conflict_not_uncertain",)
        if latest is None or latest.manual_review_required is not True:
            return ("same_record_conflict_without_manual_review",)
        return ()

    return ("unknown_temporal_relation",)


def _claim_evidence_violations(
    claim: ClaimRef | None,
    snapshot: VerdictSnapshot,
    evidence: tuple[ClaimEvidence, ...],
) -> tuple[str, ...]:
    """Validate facts for one positive snapshot without trusting its verdict."""

    violations: list[str] = []
    if claim is None:
        return ("confirmation_without_claim_ref",)
    if (
        not claim.claim_id
        or not claim.attempt_ref
        or not claim.session_ref
        or not claim.profile_ref
        or not claim.source_tree_sha256
        or not claim.target_ref
        or not claim.expected_effect_sha256
        or not claim.window_start_ref
        or claim.dependency_scope is None
    ):
        violations.append("confirmation_incomplete_claim_ref")
    if snapshot.claim_ref_id != claim.claim_id:
        violations.append("confirmation_claim_ref_mismatch")
    if claim.principal_ref is None:
        violations.append("confirmation_without_authenticated_principal")

    matching = tuple(
        item
        for item in evidence
        if item.claim_ref_id == claim.claim_id and item.phase == snapshot.phase
    )
    if not matching:
        return tuple((*violations, "confirmation_without_claim_evidence"))
    # More than one contradictory witness is not resolved by choosing the
    # convenient one. Every matching observation must satisfy the claim.
    for item in matching:
        if item.attempt_ref != claim.attempt_ref:
            violations.append("confirmation_attempt_binding_mismatch")
        if item.session_ref != claim.session_ref:
            violations.append("confirmation_session_binding_mismatch")
        if item.principal_ref is None:
            violations.append("confirmation_without_authenticated_principal")
        if item.principal_ref != claim.principal_ref:
            violations.append("confirmation_principal_binding_mismatch")
        if item.profile_ref != claim.profile_ref:
            violations.append("confirmation_profile_binding_mismatch")
        if item.source_tree_sha256 != claim.source_tree_sha256:
            violations.append("confirmation_source_binding_mismatch")
        if (
            item.profile_admitted is not True
            or item.profile_ref is None
            or item.semantics_sha256 is None
        ):
            violations.append("confirmation_without_admitted_profile")
        if item.readback_complete is not True:
            violations.append("confirmation_incomplete_readback")
        if item.readback_freshness != "authoritative":
            violations.append("confirmation_without_fresh_readback")
        if item.readback_target_ref != claim.target_ref:
            violations.append("confirmation_readback_target_mismatch")
        if item.readback_effect_sha256 != claim.expected_effect_sha256:
            violations.append("confirmation_readback_effect_mismatch")

        if claim.operation == "new_write":
            if item.effect_disposition != "new_commit":
                violations.append("confirmation_without_new_commit_disposition")
            if item.effect_target_ref != claim.target_ref:
                violations.append("confirmation_response_target_mismatch")
            if (
                item.effect_version_ref is None
                or item.readback_version_ref != item.effect_version_ref
            ):
                violations.append("confirmation_commit_version_mismatch")
        else:
            if item.effect_disposition not in ("new_commit", "already_satisfied"):
                violations.append("ensure_state_without_supported_disposition")
            if item.effect_target_ref != claim.target_ref:
                violations.append("confirmation_response_target_mismatch")
            if (
                item.effect_disposition == "already_satisfied"
                and claim.operation != "ensure_state"
            ):
                violations.append("already_satisfied_used_for_write_claim")
            if item.effect_disposition == "new_commit" and (
                item.effect_target_ref != claim.target_ref
                or item.effect_version_ref != item.readback_version_ref
            ):
                violations.append("confirmation_commit_version_mismatch")

        if (
            claim.temporal_requirement == "historical_commit"
            and claim.operation != "new_write"
        ):
            violations.append("historical_commit_requires_new_write_claim")

        if item.channel == "direct_response":
            if not all(
                (
                    item.owned_exchange_ref,
                    item.request_ref,
                    item.response_ref,
                )
            ):
                violations.append("confirmation_without_owned_exchange_pair")
        elif item.channel == "native_operation":
            if (
                item.native_operation_ref is None
                or item.owned_exchange_ref is None
                or item.request_ref is None
            ):
                violations.append("confirmation_without_native_operation_join")
        elif item.channel == "trusted_log":
            if (
                item.native_operation_ref is None
                or item.owned_exchange_ref is None
                or item.request_ref is None
            ):
                violations.append("confirmation_without_trusted_log_join")
        elif item.channel == "quiescent_diff":
            if item.window_complete is not True or item.owned_exchange_ref is None:
                violations.append("confirmation_without_quiescent_dispatch_fence")
        else:
            violations.append("confirmation_with_unsupported_evidence_channel")

        if claim.temporal_requirement == "through_barrier" and (
            claim.barrier_ref is None
            or claim.window_end_ref is None
            or item.window_complete is not True
        ):
            violations.append("confirmation_without_complete_barrier_window")
        if (
            claim.temporal_requirement in ("current_state", "through_barrier")
            and item.readback_semantics != "authoritative_current"
        ):
            violations.append("confirmation_without_current_read_semantics")
        if (
            claim.temporal_requirement == "current_state"
            and item.readback_version_ref is None
        ):
            violations.append("confirmation_without_current_version")
    return tuple(violations)


def _has_complete_refutation_read(
    claim: ClaimRef | None,
    snapshot: VerdictSnapshot,
    evidence: tuple[ClaimEvidence, ...],
) -> bool:
    """Require an actual complete, authoritative read to support REFUTED.

    A bounded or otherwise unreadable list response cannot establish absence.
    The adapter must report the read facts for this claim and phase; scenario
    labels and the verifier's REFUTED outcome are not completeness evidence.
    """

    if claim is None:
        return False
    matching = tuple(
        item
        for item in evidence
        if item.claim_ref_id == claim.claim_id and item.phase == snapshot.phase
    )
    return bool(matching) and all(
        item.readback_complete is True
        and item.readback_freshness == "authoritative"
        and item.readback_target_ref == claim.target_ref
        for item in matching
    )


def _refutation_read_matches_claim_time(
    claim: ClaimRef | None,
    snapshot: VerdictSnapshot,
    evidence: tuple[ClaimEvidence, ...],
) -> bool:
    """Check that a complete read can refute the claim's temporal scope."""

    if claim is None:
        return False
    matching = tuple(
        item
        for item in evidence
        if item.claim_ref_id == claim.claim_id and item.phase == snapshot.phase
    )
    if not matching:
        return False
    if claim.temporal_requirement == "current_state":
        return all(
            item.readback_semantics == "authoritative_current" for item in matching
        )
    if claim.temporal_requirement == "historical_commit":
        return bool(claim.required_version_ref) and all(
            item.readback_semantics == "immutable_version"
            and item.readback_version_ref == claim.required_version_ref
            for item in matching
        )
    # ClaimEvidence has no observed reference proving which barrier was crossed.
    return False


def _has_authoritative_refutation_reader(
    claim: ClaimRef | None,
    snapshot: VerdictSnapshot,
    evidence: tuple[ClaimEvidence, ...],
) -> bool:
    """Require the complete read to come from its source-bound admitted profile.

    This is reader authority, separate from the actuator principal. A complete
    response with no admitted semantics/source binding cannot irreversibly
    seed a claim's REFUTED state.
    """

    if (
        claim is None
        or not _has_complete_refutation_read(claim, snapshot, evidence)
        or not _refutation_read_matches_claim_time(claim, snapshot, evidence)
    ):
        return False
    if not claim.source_tree_sha256 or not claim.profile_ref:
        return False
    matching = tuple(
        item
        for item in evidence
        if item.claim_ref_id == claim.claim_id and item.phase == snapshot.phase
    )
    return bool(matching) and all(
        item.source_tree_sha256 == claim.source_tree_sha256
        and item.profile_ref == claim.profile_ref
        and item.profile_admitted is True
        and bool(item.semantics_sha256)
        and item.channel is not None
        for item in matching
    )


def temporal_violations(
    scenario: TemporalScenario, observation: TemporalObservation
) -> tuple[str, ...]:
    """Check actual adapter facts against shared attribution/scope invariants.

    Scenario events are the test's external ground truth, not verifier
    evidence. In particular, an ``authorized`` event never sets
    ``verifier_attributed_effect`` by itself. Returned strings are fixed codes;
    they contain no adapter exception or fixture data.
    """

    del scenario  # Inputs are never substituted for observed evidence.
    violations: list[str] = []
    if observation.write_count != sum(
        mutation.committed for mutation in observation.mutations
    ):
        violations.append("mutation_count_mismatch")

    snapshots = observation.verdicts
    any_confirmed = any(
        snapshot.canonical_outcome == "confirmed"
        or snapshot.reply_outcome == "confirmed"
        or snapshot.reply_verified is True
        for snapshot in snapshots
    )
    if any_confirmed:
        claim = observation.claim_ref
        # Legacy observations remain under the original strict new-write rule.
        # Only an actual source-derived ensure_state ClaimRef can express the
        # distinct no-new-write success semantics.
        if claim is None or claim.operation == "new_write":
            if observation.observed_dispatch_effect is not True:
                violations.append("confirmation_without_observed_dispatch_effect")
            if observation.verifier_attributed_effect is not True:
                violations.append("confirmation_without_verifier_attribution")
        has_read_scope = any(
            witness.read_performed for witness in observation.scope_witnesses
        )
        has_claim_readback = claim is not None and all(
            any(
                item.claim_ref_id == claim.claim_id
                and item.phase == snapshot.phase
                and item.readback_complete is True
                and item.readback_freshness == "authoritative"
                and item.readback_target_ref == claim.target_ref
                for item in observation.claim_evidence
            )
            for snapshot in snapshots
            if snapshot.canonical_outcome == "confirmed"
            or snapshot.reply_outcome == "confirmed"
            or snapshot.reply_verified is True
        )
        if not has_read_scope and not has_claim_readback:
            violations.append("confirmation_without_read_scope")
        for snapshot in snapshots:
            if not (
                snapshot.canonical_outcome == "confirmed"
                or snapshot.reply_outcome == "confirmed"
                or snapshot.reply_verified is True
            ):
                continue
            violations.extend(
                _claim_evidence_violations(claim, snapshot, observation.claim_evidence)
            )

    witnesses = {
        witness.read_index: witness
        for witness in observation.scope_witnesses
        if witness.read_performed and witness.read_index is not None
    }
    for witness in observation.scope_witnesses:
        if not witness.read_performed:
            if any_confirmed:
                violations.append("confirmation_incomplete_scope")
            continue
        participant_count = len(witness.participating_config_sha256)
        if (
            witness.read_config_sha256 is None
            or witness.read_scope_sha256 is None
            or participant_count == 0
            or len(witness.participating_scope_sha256) != participant_count
            or witness.read_exemptions is None
            or len(witness.participating_exemptions) != participant_count
        ):
            if any_confirmed:
                violations.append("confirmation_unknown_scope")
            continue
        if any(
            witness.read_scope_sha256 != participant_scope
            for participant_scope in witness.participating_scope_sha256
        ):
            violations.append("read_scope_mismatch")
            if any_confirmed:
                violations.append("confirmation_scope_mismatch")
            continue
        read_exemptions = set(witness.read_exemptions)
        exemptions_broader = any(
            not read_exemptions.issubset(set(contract_exemptions))
            for contract_exemptions in witness.participating_exemptions
        )
        if exemptions_broader:
            violations.append("read_exemptions_broader")
            if any_confirmed:
                violations.append("confirmation_exemptions_broader")

    for gap in observation.gaps:
        if gap.before_read_index is None or gap.after_read_index is None:
            if gap.clean is True:
                violations.append("clean_gap_missing_snapshot")
            continue
        before = witnesses.get(gap.before_read_index)
        after = witnesses.get(gap.after_read_index)
        if before is None or after is None:
            if gap.clean is True:
                violations.append("clean_gap_missing_snapshot")
            continue
        before_tables = {table.table_id: table for table in before.tables}
        after_tables = {table.table_id: table for table in after.tables}
        read_shape_changed = set(before_tables) != set(after_tables)
        if not read_shape_changed:
            for table_id, first in before_tables.items():
                later = after_tables[table_id]
                if first.coverage == "unknown" or later.coverage == "unknown":
                    read_shape_changed = True
                    break
                if (
                    first.mode,
                    first.columns,
                    first.cell_columns,
                    first.row_cap,
                    first.coverage,
                    first.row_hash_format,
                ) != (
                    later.mode,
                    later.columns,
                    later.cell_columns,
                    later.row_cap,
                    later.coverage,
                    later.row_hash_format,
                ):
                    read_shape_changed = True
                    break
        if read_shape_changed:
            if gap.clean is True:
                violations.append("representation_mismatch_clean")
            if gap.unread is not True:
                violations.append("representation_mismatch_not_unread")

    refuted_seen = False
    refuted_claim_ids: set[str] = set()
    for snapshot in snapshots:
        claim_id = snapshot.claim_ref_id
        if claim_id is None and observation.claim_ref is not None:
            claim_id = observation.claim_ref.claim_id
        same_claim_refuted = (
            claim_id in refuted_claim_ids if claim_id is not None else refuted_seen
        )
        reports_refuted = (
            snapshot.reply_outcome == "refuted"
            or snapshot.canonical_outcome == "refuted"
            or snapshot.durable_outcome == "refuted"
        )
        complete_refutation_read = _has_complete_refutation_read(
            observation.claim_ref, snapshot, observation.claim_evidence
        )
        if reports_refuted and not complete_refutation_read:
            violations.append("refutation_without_complete_read")
        elif reports_refuted and not _refutation_read_matches_claim_time(
            observation.claim_ref, snapshot, observation.claim_evidence
        ):
            violations.append("refutation_temporal_scope_mismatch")
        authoritative_refutation_reader = _has_authoritative_refutation_reader(
            observation.claim_ref, snapshot, observation.claim_evidence
        )
        # A complete one-shot read may truthfully contradict the desired state,
        # but it cannot seed an irreversible claim refutation without a
        # source-bound reader. Flag only an actual durable promotion here.
        if (
            snapshot.durable_outcome == "refuted"
            and not authoritative_refutation_reader
        ):
            violations.append("refutation_without_authoritative_reader")
        current_refuted = (
            snapshot.canonical_outcome == "refuted"
            or snapshot.durable_outcome == "refuted"
        ) and authoritative_refutation_reader
        if same_claim_refuted and snapshot.canonical_outcome == "uncertain":
            violations.append("later_canonical_uncertainty_erased_refutation")
        if (
            same_claim_refuted
            and observation.ledger_present is True
            and snapshot.durable_outcome is not None
            and snapshot.durable_outcome != "refuted"
        ):
            violations.append("later_durable_outcome_erased_refutation")
        claims_confirmation = (
            snapshot.canonical_outcome == "confirmed"
            or snapshot.durable_outcome == "confirmed"
            or snapshot.reply_outcome == "confirmed"
            or snapshot.reply_verified is True
        )
        if same_claim_refuted and claims_confirmation:
            violations.append("later_confirmation_erased_refutation")
        if current_refuted and (
            snapshot.reply_outcome == "confirmed" or snapshot.reply_verified is True
        ):
            violations.append("reply_confirmation_with_refutation")
        refuted_seen = refuted_seen or current_refuted
        if current_refuted and claim_id is not None:
            refuted_claim_ids.add(claim_id)

    return tuple(violations)


def assert_temporal_invariants(
    scenario: TemporalScenario, observation: TemporalObservation
) -> None:
    """Raise once with every fixed-code violation in an actual observation."""

    violations = temporal_violations(scenario, observation)
    if violations:
        raise TemporalInvariantError(", ".join(violations))


def build_temporal_state_machine(
    run_scenario: Callable[[TemporalScenario], TemporalObservation],
    *,
    initial_targets: Sequence[Literal["absent", "present"]] = ("absent", "present"),
    intents: Sequence[Literal["create", "update"]] = ("create",),
    stages: Sequence[Phase] = (
        "before_begin",
        "baseline_read",
        "after_begin",
        "pre_dispatch",
        "dispatch",
        "post_dispatch",
        "before_end",
        "check_read",
        "settle",
        "after_end",
        "recheck",
        "final_gap",
    ),
    actors: Sequence[Actor] = ("authorized", "foreign"),
    effects: Sequence[Effect] = (
        "noop",
        "set_target",
        "remove_target",
        "set_collateral",
        "revert_target",
    ),
    scope_variants: Sequence[ScopeVariant] = (),
    faults: Sequence[TemporalFault] = (),
    initial_table_rows: Sequence[int | None] = (None,),
    row_caps: Sequence[int | None] = (None,),
    max_events: int = 5,
) -> type:
    """Build one Hypothesis state machine around a real scenario adapter.

    The generated rules construct a bounded ordered timeline. The final rule
    executes that complete timeline once through the supplied real API; the
    invariant checks only the adapter's returned observations. Adapters must
    raise :class:`TemporalAdapterUnavailable` rather than silently dropping an
    unsupported event, fault, or scope variant.
    """

    from hypothesis import strategies as st
    from hypothesis.stateful import (
        RuleBasedStateMachine,
        initialize,
        invariant,
        precondition,
        rule,
    )

    class TemporalStateMachine(RuleBasedStateMachine):
        @initialize(
            initial_target=st.sampled_from(tuple(initial_targets)),
            intent=st.sampled_from(tuple(intents)),
            table_rows=st.sampled_from(tuple(initial_table_rows)),
            row_cap=st.sampled_from(tuple(row_caps)),
        )
        def start(self, initial_target, intent, table_rows, row_cap):
            self.scenario = TemporalScenario(
                initial_target=initial_target,
                intent=intent,
                initial_table_rows=table_rows,
                row_cap=row_cap,
            )
            self._events = []
            self._faults = []
            self._scope_variants = []
            self._observation = None

        @precondition(
            lambda self: self._observation is None and len(self._events) < max_events
        )
        @rule(
            stage=st.sampled_from(tuple(stages)),
            actor=st.sampled_from(tuple(actors)),
            effect=st.sampled_from(tuple(effects)),
        )
        def add_event(self, stage, actor, effect):
            self._events.append(TemporalEvent(stage, actor, effect))
            self.scenario = TemporalScenario(
                initial_target=self.scenario.initial_target,
                intent=self.scenario.intent,
                events=tuple(self._events),
                faults=tuple(self._faults),
                participating_scope_variants=tuple(self._scope_variants),
                initial_table_rows=self.scenario.initial_table_rows,
                row_cap=self.scenario.row_cap,
            )

        @precondition(
            lambda self: (
                self._observation is None
                and bool(scope_variants)
                and len(self._scope_variants) < max_events
            )
        )
        @rule(
            variant=(
                st.sampled_from(tuple(scope_variants))
                if scope_variants
                else st.nothing()
            )
        )
        def add_scope_variant(self, variant):
            self._scope_variants.append(variant)
            self._refresh_scenario()

        @precondition(
            lambda self: (
                self._observation is None
                and bool(faults)
                and len(self._faults) < max_events
            )
        )
        @rule(fault=st.sampled_from(tuple(faults)) if faults else st.nothing())
        def add_fault(self, fault):
            self._faults.append(fault)
            self._refresh_scenario()

        def _refresh_scenario(self):
            self.scenario = TemporalScenario(
                initial_target=self.scenario.initial_target,
                intent=self.scenario.intent,
                events=tuple(self._events),
                faults=tuple(self._faults),
                participating_scope_variants=tuple(self._scope_variants),
                initial_table_rows=self.scenario.initial_table_rows,
                row_cap=self.scenario.row_cap,
            )

        @precondition(lambda self: self._observation is None)
        @rule()
        def execute_scenario(self):
            self._observation = run_scenario(self.scenario)
            assert_temporal_invariants(self.scenario, self._observation)

        @invariant()
        def actual_observation_satisfies_model(self):
            if self._observation is not None:
                assert_temporal_invariants(self.scenario, self._observation)

        @precondition(lambda self: self._observation is not None)
        @rule()
        def retain_terminal_observation(self):
            assert_temporal_invariants(
                self.scenario, cast(TemporalObservation, self._observation)
            )

        def teardown(self):
            if self._observation is None:
                self._observation = run_scenario(self.scenario)
            assert_temporal_invariants(self.scenario, self._observation)

    return TemporalStateMachine
