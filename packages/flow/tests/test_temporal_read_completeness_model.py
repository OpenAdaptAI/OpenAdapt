"""Claim-scoped refutation rules for complete and incomplete REST reads."""

from __future__ import annotations

from tests.e2e.temporal_observation_protocol import (
    ClaimEvidence,
    ClaimRef,
    Phase,
    ReadbackSemantics,
    TemporalObservation,
    TemporalRequirement,
    TemporalScenario,
    VerdictSnapshot,
    temporal_violations,
)


def _observation(
    *,
    complete: bool | None,
    phase: Phase = "settle",
    temporal_requirement: TemporalRequirement = "current_state",
    readback_semantics: ReadbackSemantics = "authoritative_current",
    readback_version_ref: str | None = "version-1",
    window_complete: bool | None = None,
    profile_admitted: bool | None = None,
    durable_outcome: str | None = None,
) -> TemporalObservation:
    claim = ClaimRef(
        claim_id="claim-1",
        attempt_ref="attempt-1",
        session_ref="session-1",
        principal_ref=None,
        profile_ref="profile-1",
        source_tree_sha256="source-1",
        operation="new_write",
        temporal_requirement=temporal_requirement,
        target_ref="target-1",
        dependency_scope=("target-1",),
        expected_effect_sha256="effect-1",
        window_start_ref="window-1",
        window_end_ref=(
            "window-end-1" if temporal_requirement == "through_barrier" else None
        ),
        required_version_ref=(
            "version-1" if temporal_requirement == "historical_commit" else None
        ),
        barrier_ref=(
            "barrier-1" if temporal_requirement == "through_barrier" else None
        ),
    )
    evidence = ClaimEvidence(
        phase=phase,
        claim_ref_id="claim-1",
        attempt_ref="attempt-1",
        session_ref="session-1",
        principal_ref=None,
        source_tree_sha256="source-1",
        channel="direct_response",
        profile_ref="profile-1",
        profile_admitted=profile_admitted,
        semantics_sha256=("semantics-1" if profile_admitted else None),
        owned_exchange_ref=None,
        request_ref=None,
        response_ref=None,
        effect_disposition=None,
        effect_target_ref=None,
        effect_version_ref=None,
        native_operation_ref=None,
        readback_semantics=readback_semantics,
        readback_target_ref="target-1",
        readback_version_ref=readback_version_ref,
        readback_effect_sha256=None,
        readback_freshness="authoritative",
        readback_complete=complete,
        window_complete=window_complete,
    )
    snapshot = VerdictSnapshot(
        phase=phase,
        transport_status=None,
        reply_state=None,
        reply_outcome="refuted",
        reply_verified=False,
        canonical_outcome="refuted",
        frame_verdict=None,
        durable_outcome=durable_outcome,  # type: ignore[arg-type]
        durable_state="held" if durable_outcome else None,
        claim_ref_id="claim-1",
    )
    return TemporalObservation(
        phase_trace=(phase,),
        begin_reached=True,
        mutations=(),
        write_count=0,
        verdicts=(snapshot,),
        scope_witnesses=(),
        gaps=(),
        observed_dispatch_effect=None,
        verifier_attributed_effect=None,
        ledger_present=durable_outcome is not None,
        observed_target_state="absent",
        claim_ref=claim,
        claim_evidence=(evidence,),
    )


def test_incomplete_or_unknown_refutation_is_never_accepted() -> None:
    scenario = TemporalScenario(initial_target="absent", intent="create")
    for completeness in (False, None):
        violations = temporal_violations(scenario, _observation(complete=completeness))
        assert "refutation_without_complete_read" in violations


def test_complete_raw_refutation_without_admitted_reader_is_one_shot() -> None:
    scenario = TemporalScenario(initial_target="absent", intent="create")
    # This synthetic claim explicitly requires current state. It does not
    # relabel the actual REST new-write observer as a current-state claim.
    observation = _observation(
        complete=True,
        temporal_requirement="current_state",
        readback_semantics="authoritative_current",
        profile_admitted=None,
    )

    violations = temporal_violations(scenario, observation)
    assert violations == ()
    assert observation.verdicts[0].canonical_outcome == "refuted"
    assert observation.verdicts[0].durable_outcome is None
    assert observation.ledger_present is False


def test_unadmitted_refutation_cannot_be_promoted_to_durable_state() -> None:
    scenario = TemporalScenario(initial_target="absent", intent="create")
    observation = _observation(
        complete=True, profile_admitted=None, durable_outcome="refuted"
    )

    violations = temporal_violations(scenario, observation)
    assert "refutation_without_authoritative_reader" in violations


def test_current_absence_cannot_refute_historical_commit() -> None:
    scenario = TemporalScenario(initial_target="absent", intent="create")
    observation = _observation(
        complete=True,
        temporal_requirement="historical_commit",
        readback_semantics="authoritative_current",
        profile_admitted=True,
        durable_outcome="refuted",
    )

    violations = temporal_violations(scenario, observation)
    assert "refutation_without_complete_read" not in violations
    assert "refutation_temporal_scope_mismatch" in violations
    assert "refutation_without_authoritative_reader" in violations


def test_historical_refutation_requires_the_claimed_immutable_version() -> None:
    scenario = TemporalScenario(initial_target="absent", intent="create")
    phases: tuple[Phase, ...] = ("settle", "recheck")
    for phase in phases:
        exact = _observation(
            complete=True,
            phase=phase,
            temporal_requirement="historical_commit",
            readback_semantics="immutable_version",
            readback_version_ref="version-1",
        )
        wrong_version = _observation(
            complete=True,
            phase=phase,
            temporal_requirement="historical_commit",
            readback_semantics="immutable_version",
            readback_version_ref="version-other",
        )

        assert "refutation_temporal_scope_mismatch" not in temporal_violations(
            scenario, exact
        )
        assert "refutation_temporal_scope_mismatch" in temporal_violations(
            scenario, wrong_version
        )


def test_current_state_refutation_rejects_immutable_version_only_read() -> None:
    scenario = TemporalScenario(initial_target="absent", intent="create")
    observation = _observation(
        complete=True,
        temporal_requirement="current_state",
        readback_semantics="immutable_version",
        readback_version_ref="version-1",
    )

    assert "refutation_temporal_scope_mismatch" in temporal_violations(
        scenario, observation
    )


def test_through_barrier_refutation_needs_an_observed_matching_barrier() -> None:
    scenario = TemporalScenario(initial_target="absent", intent="create")
    observation = _observation(
        complete=True,
        phase="recheck",
        temporal_requirement="through_barrier",
        readback_semantics="authoritative_current",
        window_complete=True,
    )

    assert "refutation_temporal_scope_mismatch" in temporal_violations(
        scenario, observation
    )
