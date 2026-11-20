"""Pure synthetic controls. No SDK compilation, COM object or native call."""

from dataclasses import replace
from itertools import product
from pathlib import Path

import pytest
from capability_model import (
    PROTOTYPE_CONTRACT,
    S_OK,
    UIA_E_NOTSUPPORTED,
    Capability,
    Checkpoint,
    CoverageClaim,
    Observation,
    Output,
    Pattern,
    RawReply,
    Reason,
    Scope,
    Stage,
    assess_model_coverage,
    observe_model,
)
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

SCOPE = Scope(b"x" * 32, 1)
ALL = frozenset(Pattern)


class Transport:
    def __init__(
        self, acquire=RawReply(0, Output.TYPED), query=RawReply(0, Output.TYPED)
    ):
        self.acquisition = acquire
        self.query = query
        self.events = []
        self.fail_release = None

    def acquire(self, pattern):
        self.events.append(("acquire", pattern))
        if isinstance(self.acquisition, Exception):
            raise self.acquisition
        return self.acquisition

    def query_exact_interface(self, pattern):
        self.events.append(("query", pattern))
        if isinstance(self.query, Exception):
            raise self.query
        return self.query

    def release(self, slot):
        self.events.append(("release", slot))
        if self.fail_release == slot:
            raise RuntimeError("synthetic cleanup failure")


def run(
    pattern,
    *,
    transport=None,
    fail_stage=None,
    status=False,
    scope=SCOPE,
    contract=PROTOTYPE_CONTRACT,
):
    transport = transport or Transport()
    stages = []

    def checkpoint(stage):
        stages.append(stage)
        if stage == fail_stage:
            return Checkpoint(Scope(b"y" * 32, 2), status)
        return Checkpoint(SCOPE, status)

    result = observe_model(
        pattern, scope, contract=contract, checkpoint=checkpoint, transport=transport
    )
    return result, transport.events, stages


@pytest.mark.parametrize("pattern", Pattern)
def test_support_has_exact_two_references_and_final_scope_check(pattern):
    result, events, stages = run(pattern)
    assert result.capability is Capability.SUPPORTED
    assert result.reason is Reason.SUPPORTED
    assert events == [
        ("acquire", pattern),
        ("query", pattern),
        ("release", "queried"),
        ("release", "acquired"),
    ]
    assert stages == [
        Stage.BEFORE_ACQUIRE,
        Stage.BEFORE_QUERY_INTERFACE,
        Stage.AFTER_QUERY_INTERFACE,
        Stage.AFTER_RELEASE,
    ]


@pytest.mark.parametrize("pattern", Pattern)
@pytest.mark.parametrize(
    "raw,capability,reason",
    [
        (
            RawReply(UIA_E_NOTSUPPORTED, Output.NULL),
            Capability.EXPLICITLY_UNSUPPORTED,
            Reason.EXPLICITLY_UNSUPPORTED,
        ),
        (RawReply(S_OK, Output.NULL), Capability.UNKNOWN, Reason.NORMAL_NULL),
        (RawReply(1, Output.NULL), Capability.UNKNOWN, Reason.ACQUISITION),
        (RawReply(-2147467259, Output.NULL), Capability.UNKNOWN, Reason.ACQUISITION),
        (
            RawReply(UIA_E_NOTSUPPORTED, Output.TYPED),
            Capability.UNKNOWN,
            Reason.CONTRADICTORY_OUTPUT,
        ),
        (RawReply(-2147467262, Output.TYPED), Capability.UNKNOWN, Reason.ACQUISITION),
        (RawReply(S_OK, Output.INVALID), Capability.UNKNOWN, Reason.INVALID_REPLY),
        (RawReply(True, Output.TYPED), Capability.UNKNOWN, Reason.INVALID_REPLY),
        (None, Capability.UNKNOWN, Reason.INVALID_REPLY),
        (False, Capability.UNKNOWN, Reason.INVALID_REPLY),
    ],
)
def test_acquisition_outcomes_do_not_infer_absence(pattern, raw, capability, reason):
    result, events, stages = run(pattern, transport=Transport(acquire=raw))
    assert (result.capability, result.reason) == (capability, reason)
    assert not any(event[0] == "query" for event in events)
    if type(raw) is RawReply and raw.output is Output.TYPED:
        assert events[-1] == ("release", "acquired")
    assert len(stages) == (2 if capability is Capability.EXPLICITLY_UNSUPPORTED else 1)


@pytest.mark.parametrize("pattern", Pattern)
@pytest.mark.parametrize(
    "reply",
    [
        RawReply(0, Output.NULL),
        RawReply(1, Output.TYPED),
        RawReply(UIA_E_NOTSUPPORTED, Output.NULL),
        RawReply(-2147467262, Output.NULL),
        RawReply(False, Output.TYPED),
        RawReply(0, Output.INVALID),
        None,
    ],
)
def test_qi_failure_never_means_pattern_absent(pattern, reply):
    result, events, stages = run(pattern, transport=Transport(query=reply))
    assert result.capability is Capability.UNKNOWN
    assert result.reason in {Reason.QUERY_INTERFACE, Reason.INVALID_REPLY}
    assert events[-1] == ("release", "acquired")
    assert stages == [Stage.BEFORE_ACQUIRE, Stage.BEFORE_QUERY_INTERFACE]
    if type(reply) is RawReply and reply.output is Output.TYPED:
        assert events[-2] == ("release", "queried")


@pytest.mark.parametrize("pattern", Pattern)
@pytest.mark.parametrize(
    "stage",
    [
        Stage.BEFORE_ACQUIRE,
        Stage.BEFORE_QUERY_INTERFACE,
        Stage.AFTER_QUERY_INTERFACE,
        Stage.AFTER_RELEASE,
    ],
)
def test_scope_replacement_stops_without_diagnostic_read(pattern, stage):
    result, events, stages = run(pattern, fail_stage=stage)
    assert result.reason is Reason.SCOPE_OR_GUARD
    assert result.capability is Capability.UNKNOWN
    assert stages[-1] is stage
    assert (
        len(stages)
        == [
            Stage.BEFORE_ACQUIRE,
            Stage.BEFORE_QUERY_INTERFACE,
            Stage.AFTER_QUERY_INTERFACE,
            Stage.AFTER_RELEASE,
        ].index(stage)
        + 1
    )
    if stage is Stage.BEFORE_ACQUIRE:
        assert events == []
    elif stage is Stage.BEFORE_QUERY_INTERFACE:
        assert events == [("acquire", pattern), ("release", "acquired")]
    else:
        assert events[-2:] == [("release", "queried"), ("release", "acquired")]


@pytest.mark.parametrize("pattern", Pattern)
@pytest.mark.parametrize("status", [True, None, 0, 1, "False", object()])
def test_sensitive_unknown_nonbool_refuse_before_acquisition(pattern, status):
    result, events, stages = run(pattern, status=status)
    assert result.reason is Reason.SCOPE_OR_GUARD
    assert events == []
    assert stages == [Stage.BEFORE_ACQUIRE]


@pytest.mark.parametrize("pattern", Pattern)
def test_explicit_unsupported_needs_fresh_scope(pattern):
    result, events, stages = run(
        pattern,
        transport=Transport(acquire=RawReply(UIA_E_NOTSUPPORTED, Output.NULL)),
        fail_stage=Stage.AFTER_UNSUPPORTED,
    )
    assert result.capability is Capability.UNKNOWN
    assert stages == [Stage.BEFORE_ACQUIRE, Stage.AFTER_UNSUPPORTED]
    assert events == [("acquire", pattern)]


@pytest.mark.parametrize("stage", Stage)
def test_checkpoint_exception_is_closed_and_not_stringified(stage):
    class OpaqueError(Exception):
        def __str__(self):
            raise AssertionError("must not inspect provider exception")

    stages = []
    transport = (
        Transport(acquire=RawReply(UIA_E_NOTSUPPORTED, Output.NULL))
        if stage is Stage.AFTER_UNSUPPORTED
        else Transport()
    )

    def checkpoint(current):
        stages.append(current)
        if current is stage:
            raise OpaqueError()
        return Checkpoint(SCOPE, False)

    result = observe_model(
        Pattern.VALUE,
        SCOPE,
        contract=PROTOTYPE_CONTRACT,
        checkpoint=checkpoint,
        transport=transport,
    )
    assert result.capability is Capability.UNKNOWN
    assert result.reason is Reason.SCOPE_OR_GUARD
    assert stages[-1] is stage


@pytest.mark.parametrize("slot", ["queried", "acquired"])
def test_release_failure_cleans_both_references_and_emits_no_positive(slot):
    transport = Transport()
    transport.fail_release = slot
    result, events, stages = run(Pattern.VALUE, transport=transport)
    assert result.capability is Capability.UNKNOWN
    assert result.reason is Reason.RELEASE
    assert events[-2:] == [("release", "queried"), ("release", "acquired")]
    assert Stage.AFTER_RELEASE not in stages


@pytest.mark.parametrize("bad", ["", "openadapt.native-value-read/v3", None, 1, True])
def test_contract_mismatch_has_zero_checkpoint_or_transport_calls(bad):
    result, events, stages = run(Pattern.VALUE, contract=bad)
    assert result.reason is Reason.CONTRACT
    assert not events and not stages


@given(
    hr=st.integers(min_value=-(2**40), max_value=2**40),
    pattern=st.sampled_from(list(Pattern)),
    output=st.sampled_from(list(Output)),
)
def test_only_exact_signed_hresult_can_be_classified(pattern, hr, output):
    result, events, _ = run(pattern, transport=Transport(acquire=RawReply(hr, output)))
    if result.capability is Capability.SUPPORTED:
        assert hr == 0 and output is Output.TYPED
    elif result.capability is Capability.EXPLICITLY_UNSUPPORTED:
        assert hr == UIA_E_NOTSUPPORTED and output is Output.NULL
    if type(hr) is int and (hr < -(2**31) or hr >= 2**31):
        assert result.capability is Capability.UNKNOWN
    assert len([e for e in events if e == ("release", "acquired")]) == (
        output is Output.TYPED
    )


def observations(capabilities):
    reasons = {
        Capability.SUPPORTED: Reason.SUPPORTED,
        Capability.EXPLICITLY_UNSUPPORTED: Reason.EXPLICITLY_UNSUPPORTED,
        Capability.UNKNOWN: Reason.ACQUISITION,
    }
    return tuple(
        Observation(p, SCOPE, c, reasons[c]) for p, c in zip(Pattern, capabilities)
    )


def test_exhaustive_three_pattern_required_and_representation_model():
    subsets = [
        frozenset(p for p, selected in zip(Pattern, bits) if selected)
        for bits in product([False, True], repeat=3)
    ]
    checked = 0
    for capabilities, required, represented in product(
        product(Capability, repeat=3), subsets, subsets
    ):
        observed = observations(capabilities)
        claim = CoverageClaim(ALL, required, represented, frozenset())
        result = assess_model_coverage(observed, SCOPE, claim)
        expected = (
            Capability.UNKNOWN not in capabilities
            and all(
                p in represented
                for p, c in zip(Pattern, capabilities)
                if c is Capability.SUPPORTED
            )
            and all(
                c is Capability.SUPPORTED
                for p, c in zip(Pattern, capabilities)
                if p in required or p in represented
            )
        )
        assert result.model_consistent is expected
        if result.model_consistent:
            assert result.reason == "necessary_model_conditions_only"
        checked += 1
    assert checked == 1728


def test_three_absences_do_not_supply_universal_coverage():
    observed = observations([Capability.EXPLICITLY_UNSUPPORTED] * 3)
    # Unknown composite/custom channel remains unrepresented even with 3 absences.
    claim = CoverageClaim(ALL, frozenset(), frozenset(), frozenset({"other_state"}))
    assert not assess_model_coverage(observed, SCOPE, claim).model_consistent
    assert not assess_model_coverage(
        observed, SCOPE, replace(claim, required=frozenset({Pattern.VALUE}))
    ).model_consistent
    # Closed synthetic model consistency is deliberately not a runtime proof.
    abstract = replace(claim, other_relevant_channels=frozenset())
    result = assess_model_coverage(observed, SCOPE, abstract)
    assert result.model_consistent
    assert not hasattr(result, "complete") and not hasattr(result, "proof")


@pytest.mark.parametrize(
    "change", ["duplicate", "missing", "scope", "reason", "hidden_supported"]
)
def test_coverage_cannot_accept_rebinding_or_forged_consistency(change):
    observed = observations([Capability.SUPPORTED] * 3)
    claim = CoverageClaim(ALL, ALL, ALL, frozenset())
    if change == "duplicate":
        observed += observed[:1]
    elif change == "missing":
        observed = observed[:-1]
    elif change == "scope":
        observed = (replace(observed[0], scope=Scope(b"y" * 32, 1)), *observed[1:])
    elif change == "reason":
        observed = (replace(observed[0], reason=Reason.ACQUISITION), *observed[1:])
    else:
        claim = replace(
            claim,
            relevant=frozenset({Pattern.VALUE}),
            required=frozenset({Pattern.VALUE}),
        )
    assert not assess_model_coverage(observed, SCOPE, claim).model_consistent


class ScopeChanges(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.generation = 1

    @rule()
    def replace_then_restore_identity_with_new_generation(self):
        self.generation += 1

    @rule(pattern=st.sampled_from(list(Pattern)), stage=st.sampled_from(list(Stage)))
    def observe_after_scope_changes(self, pattern, stage):
        visits = []
        transport = (
            Transport(acquire=RawReply(UIA_E_NOTSUPPORTED, Output.NULL))
            if stage is Stage.AFTER_UNSUPPORTED
            else Transport()
        )
        current = Scope(SCOPE.identity, self.generation)

        def checkpoint(at):
            visits.append(at)
            if at is stage:
                return Checkpoint(
                    Scope(current.identity, current.generation + 1), False
                )
            return Checkpoint(current, False)

        result = observe_model(
            pattern,
            current,
            contract=PROTOTYPE_CONTRACT,
            checkpoint=checkpoint,
            transport=transport,
        )
        assert result.capability is Capability.UNKNOWN
        assert visits[-1] is stage
        # Same digest after replacement does not erase generation evidence.
        assert not assess_model_coverage(
            (result,), SCOPE, CoverageClaim(ALL, ALL, ALL, frozenset())
        ).model_consistent

    @invariant()
    def generation_never_rewinds(self):
        assert self.generation >= 1


TestScopeChanges = ScopeChanges.TestCase


def test_source_uses_sdk_types_and_has_no_runtime_importer():
    root = Path(__file__).resolve().parent
    source = (root / "sdk_capability.cpp").read_text()
    assert "GetCurrentPatternAs(" in source
    assert "IID_PPV_ARGS(&acquired.pointer)" in source
    assert "IID_PPV_ARGS(&verified.pointer)" in source
    assert "hr != S_OK" in source and "qi != S_OK" in source
    for interface in (p.value[1] for p in Pattern):
        assert f"acquire<{interface}>" in source
    # This is source-layout inspection, not ABI/error-info/native qualification.
    package = root.parents[1] / "openadapt_flow"
    imports = []
    for file in package.rglob("*.py"):
        text = file.read_text()
        if "prototypes.native_capability" in text or "sdk_capability" in text:
            imports.append(str(file))
    assert imports == []


@pytest.mark.parametrize("pattern", Pattern)
@pytest.mark.parametrize("stage", Stage)
@pytest.mark.parametrize("changed_status", [True, None])
def test_status_change_at_each_stage_stops_without_later_reads(
    pattern, stage, changed_status
):
    visits = []
    transport = (
        Transport(acquire=RawReply(UIA_E_NOTSUPPORTED, Output.NULL))
        if stage is Stage.AFTER_UNSUPPORTED
        else Transport()
    )

    def checkpoint(at):
        visits.append(at)
        return Checkpoint(SCOPE, changed_status if at is stage else False)

    result = observe_model(
        pattern,
        SCOPE,
        contract=PROTOTYPE_CONTRACT,
        checkpoint=checkpoint,
        transport=transport,
    )
    assert result.capability is Capability.UNKNOWN
    assert result.reason is Reason.SCOPE_OR_GUARD
    assert visits[-1] is stage


def test_no_truthiness_or_untyped_scope_equality_is_consulted():
    class Untrusted:
        def __bool__(self):
            raise AssertionError("truthiness is not evidence")

        def __eq__(self, other):
            raise AssertionError("untyped equality is not evidence")

    for current in (Checkpoint(SCOPE, Untrusted()), Checkpoint(Untrusted(), False)):
        transport = Transport()
        result = observe_model(
            Pattern.VALUE,
            SCOPE,
            contract=PROTOTYPE_CONTRACT,
            checkpoint=lambda stage: current,
            transport=transport,
        )
        assert result.capability is Capability.UNKNOWN
        assert transport.events == []
    corrupt = observations([Capability.SUPPORTED] * 3)
    corrupt = (replace(corrupt[0], scope=Untrusted()), *corrupt[1:])
    assert not assess_model_coverage(
        corrupt, SCOPE, CoverageClaim(ALL, ALL, ALL, frozenset())
    ).model_consistent


def test_actual_flow_package_selection_excludes_candidate_source():
    # Inspect existing checked-out package selectors, not an assumed directory rule.
    import tomllib

    directory = Path(__file__).resolve().parent
    project = directory.parents[1]
    config_path = project / "pyproject.toml"
    config = tomllib.loads(config_path.read_text())["tool"]["hatch"]["build"]["targets"]
    wheel = config["wheel"]
    assert wheel["packages"] == ["openadapt_flow"]
    assert "include" not in wheel and "only-include" not in wheel
    candidates = tuple(p for p in directory.iterdir() if p.is_file())
    assert candidates
    for file in candidates:
        assert not any(
            file.is_relative_to(project / package) for package in wheel["packages"]
        )
        for source in wheel["force-include"]:
            # All actual force-include sources are explicit local paths.
            assert not any(c in source for c in "*?[")
            assert not file.is_relative_to(project / source)
    assert "/prototypes" in config["sdist"]["exclude"]
    assert all(p.is_relative_to(project / "prototypes") for p in candidates)
    # No build hook or wheel configuration changed to insert another transport.
    build = tomllib.loads(config_path.read_text())["tool"]["hatch"]["build"]
    assert "hooks" not in build


def test_launcher_actual_package_selectors_also_exclude_candidate_source():
    import tomllib

    root = Path(__file__).resolve().parents[4]
    config = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["hatch"][
        "build"
    ]["targets"]
    assert config["wheel"]["packages"] == ["openadapt"]
    for included in config["sdist"]["include"]:
        assert not any(c in included for c in "*?[")
        assert not Path(__file__).resolve().is_relative_to(root / included.lstrip("/"))


def test_absent_value_cannot_be_represented_even_when_not_required():
    observed = observations([Capability.EXPLICITLY_UNSUPPORTED] * 3)
    claim = CoverageClaim(ALL, frozenset(), frozenset({Pattern.VALUE}), frozenset())
    assert not assess_model_coverage(observed, SCOPE, claim).model_consistent
