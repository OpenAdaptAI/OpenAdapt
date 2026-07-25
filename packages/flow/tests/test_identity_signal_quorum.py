"""Qualified identity-signal quorum behavior and PHI-free reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.identity_signals import parameterize_identity_text
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ApiBinding,
    ApiIdentityBinding,
    IdentityCheck,
    IdentitySignalEvidence,
    Resolution,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    IdentityPolicy,
    IdentitySignalPolicy,
    QualificationRefusalCode,
    evaluate_qualification,
    init_project,
    set_action_classification,
    set_identity_policy,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    verify_signal_template,
)
from openadapt_flow.runtime.replayer import Replayer


class _Backend:
    viewport = (800, 600)

    def __init__(self, structured: str | None) -> None:
        self.structured = structured

    def structured_text_at(self, _x: int, _y: int) -> str | None:
        return self.structured


class _RuntimeBackend(_Backend):
    def __init__(self, structured: str | None) -> None:
        super().__init__(structured)
        self.actions: list[tuple[object, ...]] = []

    def screenshot(self) -> bytes:
        return b"frame"

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.actions.append(("click", x, y, double))

    def press(self, key: str) -> None:
        self.actions.append(("press", key))


class _Vision:
    def ocr(self, _png: bytes, region=None):  # noqa: ANN001
        return []

    def wait_settled(self, backend: _RuntimeBackend) -> bytes:
        return backend.screenshot()


def _step() -> Step:
    return Step(
        id="save",
        intent="Save selected record",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/save.png",
            region=(20, 20, 80, 30),
            click_point=(60, 35),
            structured_identity="Alice Example account ZX-942",
            context_text="Alice Example 1970-02-03 account ZX-942",
            identifier_crop="templates/id.png",
            identifier_region=(120, 20, 100, 25),
        ),
        identity_armed=True,
        risk="irreversible",
    )


def _workflow(step: Step, policy: IdentityPolicy) -> Workflow:
    workflow = Workflow(name="identity-quorum", steps=[step])
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="citrix",
            application="Reference application",
            application_version="1",
            environment_digest="a" * 64,
            runtime_version="1.22.0",
        ),
    )
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id=step.id,
            classification=ActionRiskClass.IRREVERSIBLE,
            explanation="Changes the selected record",
            operator_confirmed=True,
        ),
    )
    set_identity_policy(workflow, policy)
    return workflow


def _resolution() -> Resolution:
    return Resolution(
        rung="template",
        point=(60, 35),
        confidence=0.99,
        elapsed_ms=1,
    )


def _replayer(structured: str | None) -> Replayer:
    return Replayer(_Backend(structured), vision=_Vision())


def test_multi_signal_success_uses_independent_live_sources(monkeypatch) -> None:
    step = _step()
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="record_id",
                source="structured",
                match="exact",
            ),
            IdentitySignalPolicy(
                key="secondary_identifier",
                source="captured_context",
                region=(0, 0, 100, 15),
                match="normalized",
                normalizers=["unicode_nfkc", "collapse_whitespace"],
            ),
        ],
        quorum=2,
    )
    workflow = _workflow(step, policy)
    replayer = _replayer("Alice Example account ZX-942")
    monkeypatch.setattr(
        replayer,
        "_captured_context_observations",
        lambda *_args: ["Alice Example\n1970-02-03 account ZX-942"],
    )

    check = replayer._verify_identity(
        step,
        _resolution(),
        b"fresh-frame",
        {},
        workflow,
        Path("."),
    )

    assert check.status == "verified"
    assert check.mode == "signal_quorum"
    assert check.quorum_verified == 2
    assert [item.verdict for item in check.signal_evidence] == [
        "verified",
        "verified",
    ]


def test_conflicting_identifier_halts_even_when_name_reaches_quorum(
    monkeypatch,
) -> None:
    step = _step()
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="subject_name",
                source="structured",
                match="normalized",
                normalizers=["collapse_whitespace"],
            ),
            IdentitySignalPolicy(
                key="record_id",
                source="captured_context",
                region=(0, 0, 100, 15),
                match="exact",
            ),
        ],
        quorum=1,
    )
    workflow = _workflow(step, policy)
    replayer = _replayer("Alice Example account ZX-942")
    monkeypatch.setattr(
        replayer,
        "_captured_context_observations",
        lambda *_args: ["Alice Example 1970-02-03 account ZX-943"],
    )
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)

    error = replayer._identity_gate_error(
        step,
        _resolution(),
        b"fresh-frame",
        {},
        workflow,
        Path("."),
        result,
    )

    assert error is not None
    assert "record_id/captured_context" in error
    assert result.safety_halt is True
    assert result.identity is not None
    assert result.identity.status == "mismatch"


def test_unreadable_signal_is_tolerated_when_other_quorum_votes_match(
    monkeypatch,
) -> None:
    step = _step()
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="application",
                source="structured",
                match="exact",
            ),
            IdentitySignalPolicy(
                key="secondary_identifier",
                source="captured_context",
                region=(0, 0, 100, 15),
                match="exact",
            ),
            IdentitySignalPolicy(
                key="record_id",
                source="identifier_region",
                region=step.anchor.identifier_region,
                match="exact",
            ),
        ],
        quorum=2,
    )
    workflow = _workflow(step, policy)
    replayer = _replayer("Alice Example account ZX-942")
    monkeypatch.setattr(
        replayer,
        "_captured_context_observations",
        lambda *_args: ["Alice Example 1970-02-03 account ZX-942"],
    )
    monkeypatch.setattr(
        replayer,
        "_identifier_crops",
        lambda *_args, **_kwargs: (None, None),
    )

    check = replayer._verify_identity(
        step, _resolution(), b"fresh", {}, workflow, Path(".")
    )

    assert check.status == "verified"
    assert check.quorum_verified == 2
    assert check.signal_evidence[-1].verdict == "unverifiable"


def test_identifier_region_requires_matching_text_and_live_pixels(
    monkeypatch,
) -> None:
    step = _step()
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="record_id",
                source="identifier_region",
                region=step.anchor.identifier_region,
                match="exact",
            )
        ],
        quorum=1,
    )
    workflow = _workflow(step, policy)
    replayer = _replayer(None)
    monkeypatch.setattr(
        replayer,
        "_identifier_crops",
        lambda *_args, **_kwargs: (b"recorded", b"live"),
    )
    monkeypatch.setattr(
        replayer,
        "_ocr_identity_crop",
        lambda png: "ZX-942" if png in {b"recorded", b"live"} else None,
    )
    monkeypatch.setattr(
        "openadapt_flow.runtime.replayer.identity_mod.verify_pixel_identity",
        lambda *_args, **_kwargs: IdentityCheck(
            status="verified",
            mode="pixel",
        ),
    )

    check = replayer._verify_identity(
        step, _resolution(), b"fresh", {}, workflow, Path(".")
    )

    assert check.status == "verified"
    assert check.signal_evidence[0].evidence_class == "recorded_and_live_region"


def test_insufficient_quorum_halts_before_actuation(monkeypatch) -> None:
    step = _step()
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="application",
                source="structured",
                match="exact",
            ),
            IdentitySignalPolicy(
                key="secondary_identifier",
                source="captured_context",
                region=(0, 0, 100, 15),
                match="exact",
            ),
        ],
        quorum=2,
    )
    workflow = _workflow(step, policy)
    replayer = _replayer("Alice Example account ZX-942")
    monkeypatch.setattr(replayer, "_captured_context_observations", lambda *_args: [""])
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)

    error = replayer._identity_gate_error(
        step,
        _resolution(),
        b"fresh",
        {},
        workflow,
        Path("."),
        result,
    )

    assert error is not None and "1/2 independent signals" in error
    assert result.safety_halt is True
    assert result.identity is not None
    assert result.identity.status == "unreadable"


def test_duplicate_source_policy_is_refused_by_qualification() -> None:
    step = _step()
    workflow = Workflow(name="duplicate-policy", steps=[step])
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Reference",
            application_version="1",
            environment_digest="b" * 64,
            runtime_version="1.22.0",
        ),
    )
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id=step.id,
            classification="irreversible",
            explanation="Consequential write",
            operator_confirmed=True,
        ),
    )
    duplicate = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="subject_name",
                source="structured",
                match="exact",
            ),
            IdentitySignalPolicy(
                key="record_id",
                source="structured",
                match="exact",
            ),
        ],
        quorum=2,
    )
    with pytest.raises(ValueError, match="not independent"):
        set_identity_policy(workflow, duplicate)

    assert workflow.qualification is not None
    workflow.qualification.identity_policies[step.id] = duplicate
    report = evaluate_qualification(workflow)
    assert QualificationRefusalCode.IDENTITY_SIGNALS_NOT_INDEPENDENT in {
        refusal.code for refusal in report.refusals
    }


def test_signal_keys_are_closed_and_cannot_carry_identity_values() -> None:
    for unsafe_name in ("Alice Example", "1970-02-03", "123456", "patient:name"):
        with pytest.raises(ValueError):
            IdentitySignalPolicy(
                key=unsafe_name,
                source="structured",
                match="exact",
            )
        with pytest.raises(ValueError):
            IdentitySignalEvidence(
                signal=unsafe_name,
                source="structured",
                verdict="verified",
                evidence_class="application_structured_text",
                match="exact",
            )


def test_captured_context_requires_explicit_region() -> None:
    with pytest.raises(ValueError, match="explicit qualified region"):
        IdentitySignalPolicy(
            key="record_id",
            source="captured_context",
            match="exact",
        )


def test_exact_and_explicit_normalized_comparisons_differ() -> None:
    step = _step()
    replayer = _replayer(None)
    exact = IdentitySignalPolicy(
        key="record_id",
        source="structured",
        match="exact",
    )
    normalized = IdentitySignalPolicy(
        key="record_id",
        source="structured",
        match="normalized",
        normalizers=["unicode_nfkc", "casefold", "collapse_whitespace"],
    )
    live = "alice example   account zx-942"

    assert (
        replayer._compare_qualified_signal_text(
            signal=exact,
            anchor=step.anchor,
            live=live,
            params={},
            workflow=Workflow(name="wf"),
        )
        == "conflict"
    )
    assert (
        replayer._compare_qualified_signal_text(
            signal=normalized,
            anchor=step.anchor,
            live=live,
            params={},
            workflow=Workflow(name="wf"),
        )
        == "verified"
    )


def test_parameterized_exact_match_does_not_silently_casefold() -> None:
    step = _step()
    replayer = _replayer(None)
    exact = IdentitySignalPolicy(
        key="record_id",
        source="structured",
        match="exact",
        params=["account"],
    )
    normalized = IdentitySignalPolicy(
        key="record_id",
        source="structured",
        match="normalized",
        normalizers=["casefold"],
        params=["account"],
    )
    workflow = Workflow(name="wf", params={"account": "ZX-942"})
    live = "Alice Example account yy-111"
    run_params = {"account": "YY-111"}

    assert (
        replayer._compare_qualified_signal_text(
            signal=exact,
            anchor=step.anchor,
            live=live,
            params=run_params,
            workflow=workflow,
        )
        == "conflict"
    )
    assert (
        replayer._compare_qualified_signal_text(
            signal=normalized,
            anchor=step.anchor,
            live=live,
            params=run_params,
            workflow=workflow,
        )
        == "verified"
    )


def test_phi_free_template_enforces_exact_and_normalized_signal_hashes() -> None:
    template = build_identity_template(
        "Alice Example account ZX-942",
        structured_identity="Alice Example account ZX-942",
        salt_hex="ab" * 16,
    )
    assert template is not None
    serialized = template.model_dump_json()
    assert "Alice Example" not in serialized
    assert "ZX-942" not in serialized

    assert (
        verify_signal_template(
            template,
            source="structured",
            match="exact",
            normalizers=[],
            live="Alice Example account ZX-942",
        )
        is True
    )
    assert (
        verify_signal_template(
            template,
            source="structured",
            match="exact",
            normalizers=[],
            live="alice example account zx-942",
        )
        is False
    )
    assert (
        verify_signal_template(
            template,
            source="structured",
            match="normalized",
            normalizers=["casefold", "collapse_whitespace"],
            live="alice example   account zx-942",
        )
        is True
    )


def test_phi_free_parameterized_signal_keeps_exact_case_semantics() -> None:
    template = build_identity_template(
        None,
        structured_identity="Alice Example account ZX-942",
        param_examples={"account": "ZX-942"},
        salt_hex="cd" * 16,
    )
    assert template is not None

    common = {
        "source": "structured",
        "live": "Alice Example account YY-111",
        "params": {"account": "YY-111"},
        "param_examples": {"account": "ZX-942"},
    }
    assert (
        verify_signal_template(
            template,
            match="exact",
            normalizers=[],
            **common,
        )
        is True
    )
    assert (
        verify_signal_template(
            template,
            match="exact",
            normalizers=[],
            **{**common, "live": "Alice Example account yy-111"},
        )
        is False
    )
    assert (
        verify_signal_template(
            template,
            match="normalized",
            normalizers=["casefold"],
            **{**common, "live": "Alice Example account yy-111"},
        )
        is True
    )


def test_signal_report_and_halt_message_do_not_contain_identity_values(
    monkeypatch,
) -> None:
    step = _step()
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="record_id",
                source="structured",
                match="exact",
            )
        ],
        quorum=1,
    )
    workflow = _workflow(step, policy)
    replayer = _replayer("Bob Different account YY-111")
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)

    error = replayer._identity_gate_error(
        step,
        _resolution(),
        b"fresh",
        {},
        workflow,
        Path("."),
        result,
    )

    payload = result.model_dump_json()
    for secret in (
        "Alice Example",
        "ZX-942",
        "Bob Different",
        "YY-111",
    ):
        assert secret not in payload
        assert error is not None and secret not in error
    assert result.identity == IdentityCheck(
        status="mismatch",
        mode="signal_quorum",
        coverage=0.0,
        signal_evidence=[
            {
                "signal": "record_id",
                "source": "structured",
                "verdict": "conflict",
                "evidence_class": "application_structured_text",
                "match": "exact",
            }
        ],
        quorum_required=1,
        quorum_verified=0,
    )


def test_consequential_enter_with_wrong_identity_halts_before_press(
    monkeypatch,
    tmp_path,
) -> None:
    base = _step()
    step = base.model_copy(
        update={"action": ActionKind.KEY, "key": "Enter", "risk": "irreversible"}
    )
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="record_id",
                source="structured",
                match="exact",
            )
        ],
        quorum=1,
    )
    workflow = _workflow(step, policy)
    backend = _RuntimeBackend("Bob Different account YY-111")
    replayer = Replayer(backend, vision=_Vision())
    monkeypatch.setattr(
        replayer,
        "_resolve_step",
        lambda *_args, **_kwargs: (_resolution(), None, None),
    )

    report = replayer.run(
        workflow,
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
    )

    assert report.success is False
    assert backend.actions == []
    assert "Identity signal quorum conflicted" in (report.results[0].error or "")


def test_qualified_api_write_requires_exact_request_effect_identity_binding() -> None:
    step = _step()
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": ValueExpr(param="patient_id")},
    )
    step.effects = [effect]
    step.api_binding = ApiBinding(
        url_template="/records",
        body_template={"patient_id": "{patient_id}"},
    )
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="record_id",
                source="structured",
                match="exact",
            )
        ],
        quorum=1,
    )
    workflow = _workflow(step, policy)
    workflow.params["patient_id"] = "p1"
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    replayer = _replayer(None)

    refusal = replayer._api_identity_refusal(
        step,
        {"patient_id": "p1"},
        workflow,
        [effect],
        result,
    )
    assert refusal is not None
    assert "no exact api_binding.identity contract" in refusal

    step.api_binding.identity = [
        ApiIdentityBinding(
            key="record_id",
            param="patient_id",
            effect_field="patient_id",
        )
    ]
    refusal = replayer._api_identity_refusal(
        step,
        {"patient_id": "p1"},
        workflow,
        [effect],
        result,
    )
    assert refusal is None
    assert result.identity is not None
    assert result.identity.status == "verified"
    assert result.identity.signal_evidence[0].source == "api_parameter"

    wrong_effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": ValueExpr(param="other_patient")},
    )
    refusal = replayer._api_identity_refusal(
        step,
        {"patient_id": "p1"},
        workflow,
        [wrong_effect],
        StepResult(step_id=step.id, intent=step.intent, ok=False),
    )
    assert refusal is not None
    assert "not bound to identity parameter" in refusal


def test_local_consequential_action_rechecks_identity_after_effect_prestate(
    monkeypatch,
    tmp_path,
) -> None:
    step = _step()
    step.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"record_id": "ZX-942"},
        )
    ]
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="record_id",
                source="structured",
                match="exact",
            )
        ],
        quorum=1,
    )
    workflow = _workflow(step, policy)
    backend = _RuntimeBackend("Alice Example account ZX-942")

    class _Verifier:
        def capture_pre_state(self):  # noqa: ANN201
            backend.structured = "Bob Different account YY-111"
            return {}

    replayer = Replayer(backend, vision=_Vision(), effect_verifier=_Verifier())
    monkeypatch.setattr(
        replayer,
        "_resolve_step",
        lambda *_args, **_kwargs: (_resolution(), None, None),
    )

    report = replayer.run(
        workflow,
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
    )

    assert report.success is False
    assert backend.actions == []
    assert "Identity signal quorum conflicted" in (report.results[0].error or "")


def test_parameter_binding_is_explicit_and_never_matches_inside_larger_value() -> None:
    parameterized, used = parameterize_identity_text(
        "Patient Johnson record",
        {"patient_name": "John"},
        names=["patient_name"],
        case_sensitive=True,
    )
    assert parameterized == "Patient Johnson record"
    assert used == []

    step = _step()
    assert step.anchor is not None
    step.anchor.structured_identity = "Patient Johnson record"
    signal = IdentitySignalPolicy(
        key="subject_name",
        source="structured",
        match="exact",
        params=["patient_name"],
    )
    verdict = _replayer(None)._compare_qualified_signal_text(
        signal=signal,
        anchor=step.anchor,
        live="Patient Johnson record",
        params={"patient_name": "John"},
        workflow=Workflow(name="boundary", params={"patient_name": "John"}),
    )
    assert verdict == "unverifiable"

    template = build_identity_template(
        None,
        structured_identity="Patient Johnson record",
        param_examples={"patient_name": "John"},
        salt_hex="ef" * 16,
    )
    assert template is not None
    assert (
        verify_signal_template(
            template,
            source="structured",
            match="exact",
            normalizers=[],
            live="Patient Johnson record",
            params={"patient_name": "John"},
            param_examples={"patient_name": "John"},
            parameter_names=["patient_name"],
        )
        is None
    )


def test_overlapping_pixel_identity_signals_cannot_form_quorum() -> None:
    step = _step()
    workflow = Workflow(name="overlap", steps=[step])
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="citrix",
            application="Reference",
            application_version="1",
            environment_digest="c" * 64,
            runtime_version="1.22.0",
        ),
    )
    policy = IdentityPolicy(
        step_id=step.id,
        signals=[
            IdentitySignalPolicy(
                key="record_id",
                source="identifier_region",
                region=(120, 20, 100, 25),
            ),
            IdentitySignalPolicy(
                key="secondary_identifier",
                source="captured_context",
                region=(160, 25, 100, 25),
            ),
        ],
        quorum=2,
    )

    with pytest.raises(ValueError, match="pixel regions overlap"):
        set_identity_policy(workflow, policy)
