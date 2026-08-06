"""Per-effect candidate verifier selection remains fail-closed."""

from __future__ import annotations

import pytest

from openadapt_flow.deployment import (
    EffectsConfig,
    build_effect_verifier,
    build_replayer,
)
from openadapt_flow.ir import ActionKind, EffectVerificationEvidence, Step, Workflow
from openadapt_flow.runtime import Replayer
from openadapt_flow.runtime.durable.approval import StateDiverged
from openadapt_flow.runtime.effects.adapter import (
    CandidateEffectVerifier,
    RedactingVerifier,
    RedactionPolicy,
    register_verifier_factory,
    verifier_identity,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ReadbackNav,
    ReadbackSpec,
    Verdict,
)
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.verification import VerificationTier


class _Verifier:
    def __init__(self, tier, *, reachable=True, name="test"):
        self.verification_tier = tier
        self.substrate = name
        self.reachable = reachable
        self.captures = 0
        self.verifies = 0

    def capture_pre_state(self):
        self.captures += 1
        return EffectState(substrate=self.substrate, reachable=self.reachable)

    def verify(self, effect, before, context=None):
        self.verifies += 1
        return EffectVerdict(
            verdict=Verdict.CONFIRMED if before.reachable else Verdict.INDETERMINATE,
            kind=effect.kind,
            substrate=self.substrate,
            matched_records=[{"patient": "private"}],
            unavailable=not before.reachable,
        )


def _effect(*, different_path=False):
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        field="note",
        value="saved",
        readback=ReadbackSpec(
            region=(0, 0, 10, 10),
            different_path=different_path,
            renavigation=(
                [
                    ReadbackNav(action="click", point=(1, 1)),
                    ReadbackNav(action="type", text="record"),
                    ReadbackNav(action="key", key="Enter"),
                ]
                if different_path
                else []
            ),
        ),
    )


def test_selection_refines_onscreen_tier_per_resolved_effect():
    onscreen = OnScreenReadbackVerifier(backend=None)
    session = _Verifier(VerificationTier.PERSISTED_STATE_REACQUISITION, name="session")
    selector = CandidateEffectVerifier([onscreen, session])
    persisted = _effect(different_path=True)
    same_surface = _effect(different_path=False)

    state = selector.capture_pre_state_for_effects([persisted, same_surface])

    assert state.for_effect(persisted).verifier is onscreen
    assert state.for_effect(same_surface).verifier is session
    assert (
        selector.verification_tier_for(persisted)
        == VerificationTier.PERSISTED_STATE_REACQUISITION
    )
    assert (
        selector.verification_tier_for(same_surface)
        == VerificationTier.PERSISTED_STATE_REACQUISITION
    )


def test_selected_candidate_pre_state_is_captured_before_verification():
    strong = _Verifier(VerificationTier.INDEPENDENT_SYSTEM, name="export")
    weak = _Verifier(VerificationTier.IMMEDIATE_SCREEN, name="screen")
    effect = _effect()
    selector = CandidateEffectVerifier([weak, strong])

    state = selector.capture_pre_state_for_effects([effect])

    assert state.reachable
    assert strong.captures == 1
    assert weak.captures == 0
    assert state.for_effect(effect).state.substrate == "export"


def test_candidate_rejects_an_opaque_pre_state_before_actuation():
    class _OpaquePreStateVerifier(_Verifier):
        def capture_pre_state(self):
            return object()

    effect = _effect()
    verifier = CandidateEffectVerifier(
        [_OpaquePreStateVerifier(VerificationTier.INDEPENDENT_SYSTEM)]
    )

    with pytest.raises(ValueError, match="invalid pre-state"):
        verifier.capture_pre_state_for_effects([effect])


def test_unavailable_selected_candidate_never_downgrades_after_actuation():
    strong = _Verifier(
        VerificationTier.INDEPENDENT_SYSTEM, reachable=False, name="export"
    )
    weak = _Verifier(VerificationTier.IMMEDIATE_SCREEN, name="screen")
    effect = _effect()
    selector = CandidateEffectVerifier([strong, weak])
    state = selector.capture_pre_state_for_effects([effect])

    verdict = selector.verify(effect, state)

    assert not state.reachable
    assert verdict.verdict is Verdict.INDETERMINATE
    assert strong.verifies == 1
    assert weak.verifies == 0


def test_different_path_onscreen_candidate_does_not_require_prestate_readability():
    """Post-action reacquisition can prove a GUI-only write without a delta."""
    effect = _effect(different_path=True)
    verifier = CandidateEffectVerifier([OnScreenReadbackVerifier(backend=None)])
    before = verifier.capture_pre_state_for_effects([effect])

    assert before.for_effect(effect).state.reachable is False
    assert (
        Replayer._required_effect_pre_state_unreadable(verifier, before, [effect])
        is False
    )


def test_prestate_requirement_stays_bound_when_candidate_tier_changes():
    class _Stateful(_Verifier):
        def requires_readable_pre_state_for(self, effect):
            return True

        def capture_pre_state(self):
            self.verification_tier = VerificationTier.IMMEDIATE_SCREEN
            return EffectState(substrate=self.substrate, reachable=False)

    effect = _effect()
    strong = _Stateful(VerificationTier.INDEPENDENT_SYSTEM, name="strong")
    weak = _Verifier(VerificationTier.INDEPENDENT_SESSION, name="weak")
    verifier = CandidateEffectVerifier([strong, weak])

    before = verifier.capture_pre_state_for_effects([effect])

    assert before.for_effect(effect).verifier is strong
    assert before.for_effect(effect).requires_readable_pre_state is True
    assert Replayer._required_effect_pre_state_unreadable(verifier, before, [effect])
    assert Replayer._candidate_binding_refusal(before, [effect]) is not None


def test_only_selected_candidate_runs_connection_preflight():
    class _Probed(_Verifier):
        def __init__(self, tier, *, ok, name):
            super().__init__(tier, name=name)
            self.ok = ok
            self.probes = 0
            self._openadapt_requires_preflight = True

        def test_connection(self, context=None):
            from openadapt_flow.runtime.effects.adapter import ConnectionProbe

            self.probes += 1
            return ConnectionProbe(
                ok=self.ok,
                substrate=self.substrate,
                reason="ready" if self.ok else "unavailable",
            )

    effect = _effect()
    selected = _Probed(VerificationTier.INDEPENDENT_SYSTEM, ok=True, name="selected")
    unselected = _Probed(VerificationTier.IMMEDIATE_SCREEN, ok=False, name="unselected")

    CandidateEffectVerifier([selected, unselected]).capture_pre_state_for_effects(
        [effect]
    )

    assert selected.probes == 1
    assert unselected.probes == 0


def test_selected_candidate_connection_failure_refuses_before_capture():
    class _Unavailable(_Verifier):
        _openadapt_requires_preflight = True

        def test_connection(self, context=None):
            from openadapt_flow.runtime.effects.adapter import ConnectionProbe

            return ConnectionProbe(
                ok=False, substrate=self.substrate, reason="unavailable"
            )

    selected = _Unavailable(VerificationTier.INDEPENDENT_SYSTEM, name="selected")

    with pytest.raises(ValueError, match="connection preflight"):
        CandidateEffectVerifier([selected]).capture_pre_state_for_effects([_effect()])

    assert selected.captures == 0


def test_bool_plugin_tier_is_rejected():
    class _CompletePlugin(_Verifier):
        def test_connection(self, context=None):
            raise AssertionError("construction must reject the invalid tier first")

        def capture_post_state(self, context=None):
            return self.capture_pre_state(context)

    register_verifier_factory(
        "candidate-bool-tier-test",
        lambda cfg, params: _CompletePlugin(True),
        replace=True,
    )
    with pytest.raises(
        ValueError, match="verification_tier must be a VerificationTier"
    ):
        build_effect_verifier(
            EffectsConfig(candidates=[EffectsConfig(kind="candidate-bool-tier-test")])
        )


def test_tier_only_plugin_is_rejected_during_candidate_construction():
    class _TierOnly:
        verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    register_verifier_factory(
        "candidate-tier-only-test", lambda cfg, params: _TierOnly(), replace=True
    )
    with pytest.raises(
        ValueError,
        match="missing test_connection, capture_pre_state, capture_post_state, verify",
    ):
        build_effect_verifier(
            EffectsConfig(candidates=[EffectsConfig(kind="candidate-tier-only-test")])
        )


def test_connection_aggregates_all_candidates_without_selection_or_raising():
    readable = _Verifier(VerificationTier.INDEPENDENT_SYSTEM, name="export")
    unavailable = _Verifier(
        VerificationTier.IMMEDIATE_SCREEN, reachable=False, name="screen"
    )
    verifier = CandidateEffectVerifier([readable, unavailable])

    probe = verifier.test_connection()

    assert probe.ok is False
    assert probe.substrate == "candidates"
    assert probe.detail["candidates"] == [
        {"substrate": "export", "ok": True, "reason": "reachable"},
        {"substrate": "screen", "ok": False, "reason": "unreachable"},
    ]
    assert readable.captures == 1
    assert unavailable.captures == 1


def test_redacting_wrapper_delegates_candidate_connection_probe():
    verifier = RedactingVerifier(
        CandidateEffectVerifier([_Verifier(VerificationTier.INDEPENDENT_SYSTEM)]),
        RedactionPolicy(),
    )

    probe = verifier.test_connection()

    assert probe.ok is True
    assert probe.substrate == "candidates"


def test_redacting_wrapper_connection_probe_never_raises():
    class _ExplodingConnection(_Verifier):
        def test_connection(self):
            raise RuntimeError("no connection")

    verifier = RedactingVerifier(
        CandidateEffectVerifier(
            [_ExplodingConnection(VerificationTier.INDEPENDENT_SYSTEM)]
        ),
        RedactionPolicy(),
    )

    probe = verifier.test_connection()

    assert probe.ok is False
    assert (
        probe.detail["candidates"][0]["reason"]
        == "connection probe raised: RuntimeError"
    )


def test_plugin_with_incomplete_lifecycle_fails_at_construction():
    class _IncompletePlugin:
        substrate = "incomplete"
        verification_tier = VerificationTier.INDEPENDENT_SYSTEM

        def capture_pre_state(self):
            return EffectState(substrate=self.substrate, reachable=True)

        def verify(self, effect, before, context=None):
            return EffectVerdict(verdict=Verdict.INDETERMINATE, kind=effect.kind)

    register_verifier_factory(
        "candidate-incomplete-plugin-test",
        lambda cfg, params: _IncompletePlugin(),
        replace=True,
    )

    with pytest.raises(ValueError, match="test_connection, capture_post_state"):
        build_effect_verifier(EffectsConfig(kind="candidate-incomplete-plugin-test"))


def test_plugin_with_incompatible_verify_signature_fails_at_construction():
    class _WrongSignature:
        substrate = "wrong-signature"
        verification_tier = VerificationTier.INDEPENDENT_SYSTEM

        def test_connection(self, context=None):
            return None

        def capture_pre_state(self, context=None):
            return EffectState(substrate=self.substrate, reachable=True)

        def capture_post_state(self, context=None):
            return self.capture_pre_state(context)

        def verify(self):
            return None

    register_verifier_factory(
        "candidate-wrong-signature-test",
        lambda cfg, params: _WrongSignature(),
        replace=True,
    )

    with pytest.raises(ValueError, match="incompatible lifecycle signature: verify"):
        build_effect_verifier(EffectsConfig(kind="candidate-wrong-signature-test"))


def test_verifier_identity_binds_config_but_not_literal_secret():
    first = build_effect_verifier(
        EffectsConfig(
            kind="fhir", base_url="https://records.example/a", access_token="one"
        )
    )
    rotated_secret = build_effect_verifier(
        EffectsConfig(
            kind="fhir", base_url="https://records.example/a", access_token="two"
        )
    )
    changed_boundary = build_effect_verifier(
        EffectsConfig(
            kind="fhir", base_url="https://records.example/b", access_token="two"
        )
    )

    assert verifier_identity(first) == verifier_identity(rotated_secret)
    assert verifier_identity(first) != verifier_identity(changed_boundary)


@pytest.mark.parametrize(
    ("retained_identity", "retained_tier"),
    [
        ("sha256:" + "a" * 64, int(VerificationTier.INDEPENDENT_SYSTEM)),
        ("sha256:" + "b" * 64, int(VerificationTier.INDEPENDENT_SESSION)),
    ],
)
def test_durable_revalidation_refuses_changed_candidate_binding(
    retained_identity, retained_tier
):
    effect = _effect()
    verifier = _Verifier(VerificationTier.INDEPENDENT_SYSTEM, name="current")
    verifier._openadapt_verifier_identity = "sha256:" + "b" * 64
    selector = CandidateEffectVerifier([verifier])
    step = Step(
        id="write",
        intent="write",
        action=ActionKind.KEY,
        key="Enter",
        effects=[effect],
    )
    evidence = EffectVerificationEvidence(
        effect_contract_hash=effect.contract_hash(),
        substrate="retained",
        verifier_identity=retained_identity,
        verification_tier=retained_tier,
        initial_verdict="confirmed",
        final_verdict="confirmed",
    )

    with pytest.raises(StateDiverged, match="identity or evidence tier changed"):
        Replayer(object(), effect_verifier=selector).revalidate_retained_effects(
            [effect],
            workflow=Workflow(name="resume", steps=[step]),
            step=step,
            actuation_path="gui",
            retained_evidence=[evidence],
        )


def test_durable_revalidation_uses_selected_current_state_readback():
    class _PersistedReadback(_Verifier):
        def __init__(self):
            super().__init__(
                VerificationTier.PERSISTED_STATE_REACQUISITION,
                reachable=False,
                name="onscreen-like",
            )
            self.current_reads = 0

        def requires_readable_pre_state_for(self, effect):
            return False

        def verify(self, effect, before, context=None):
            raise AssertionError("durable readback must use the current-state path")

        def verify_current_state(self, effect, current, context=None):
            self.current_reads += 1
            return EffectVerdict(
                verdict=Verdict.CONFIRMED,
                kind=effect.kind,
                substrate=self.substrate,
            )

    effect = _effect(different_path=True)
    verifier = _PersistedReadback()
    verifier._openadapt_verifier_identity = "sha256:" + "c" * 64
    step = Step(
        id="write",
        intent="write",
        action=ActionKind.KEY,
        key="Enter",
        effects=[effect],
    )
    evidence = EffectVerificationEvidence(
        effect_contract_hash=effect.contract_hash(),
        substrate=verifier.substrate,
        verifier_identity=verifier._openadapt_verifier_identity,
        verification_tier=int(verifier.verification_tier),
        initial_verdict="confirmed",
        final_verdict="confirmed",
    )

    Replayer(
        object(), effect_verifier=CandidateEffectVerifier([verifier])
    ).revalidate_retained_effects(
        [effect],
        workflow=Workflow(name="resume", steps=[step]),
        step=step,
        actuation_path="gui",
        retained_evidence=[evidence],
    )

    assert verifier.current_reads == 1


def test_redacting_wrapper_keeps_selected_candidate_and_redacts_evidence():
    strong = _Verifier(VerificationTier.INDEPENDENT_SYSTEM, name="export")
    effect = _effect()
    verifier = RedactingVerifier(
        CandidateEffectVerifier([strong]), RedactionPolicy(redact_fields=["patient"])
    )

    state = verifier.capture_pre_state_for_effects([effect])
    verdict = verifier.verify(effect, state)

    assert state.for_effect(effect).verifier is strong
    assert verdict.matched_records == [{"patient": "[redacted]"}]


def test_build_replayer_binds_backend_to_candidate_onscreen_verifier():
    backend = object()
    verifier = build_effect_verifier(
        EffectsConfig(candidates=[EffectsConfig(kind="onscreen")])
    )

    replayer = build_replayer(
        backend,
        allow_egress=False,
        effect_verifier=verifier,
        api_actuator=None,
        durable=False,
        use_structural=True,
    )

    assert replayer.effect_verifier is verifier
    assert isinstance(verifier, CandidateEffectVerifier)
    assert verifier._candidates[0]._backend is backend


def test_build_replayer_binds_backend_through_redacting_candidate_wrapper():
    backend = object()
    verifier = build_effect_verifier(
        EffectsConfig(
            candidates=[EffectsConfig(kind="onscreen")],
            evidence_redact_fields=["value"],
        )
    )

    replayer = build_replayer(
        backend,
        allow_egress=False,
        effect_verifier=verifier,
        api_actuator=None,
        durable=False,
        use_structural=True,
    )

    assert replayer.effect_verifier is verifier
    assert isinstance(verifier, RedactingVerifier)
    assert isinstance(verifier._inner, CandidateEffectVerifier)
    assert verifier._inner._candidates[0]._backend is backend
